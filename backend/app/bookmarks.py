from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Chunk, Manifest, Report, ReportBookmark
from .storage import get_object_store


class BookmarkError(ValueError):
    """Validation failure that maps to an HTTP error.

    `status_code` distinguishes bad caller input (422) from a bookmark whose
    frozen source is no longer resolvable (409). Stale sources must never be
    silently re-pointed at another block.
    """

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.details = details or {}


@dataclass(frozen=True)
class FrozenChunk:
    sequence: int
    sha256: str
    object_key: str
    sample_count: int
    sample_rate: float
    channels: list[str]
    start_time: str
    end_time: str


@dataclass(frozen=True)
class FrozenSegment:
    index: int
    sample_rate: float
    channels: list[str]
    chunks: list[FrozenChunk]
    total_samples: int

    @property
    def duration_seconds(self) -> float:
        return self.total_samples / self.sample_rate

    def locate(self, offset_seconds: float) -> tuple[int, int]:
        """Return (sample_index, chunk_sequence, chunk_sample_offset).

        Time is half-open: [0, total_samples/fs). The last displayed second is
        rejected instead of clamped, matching the preview segment boundary.
        """
        if not math.isfinite(offset_seconds) or offset_seconds < 0:
            raise BookmarkError(
                422,
                "bookmark_time_out_of_range",
                f"offset_seconds must be finite and >= 0, got {offset_seconds}",
            )
        duration = self.duration_seconds
        if offset_seconds >= duration:
            raise BookmarkError(
                422,
                "bookmark_time_out_of_range",
                f"offset_seconds {offset_seconds:.9g}s is outside segment {self.index} "
                f"({self.sample_rate:g} Hz), which spans 0..{duration:.9g}s",
                {
                    "segment_index": self.index,
                    "sample_rate": self.sample_rate,
                    "offset_seconds": offset_seconds,
                    "segment_duration_seconds": duration,
                },
            )
        sample_index = min(int(offset_seconds * self.sample_rate), self.total_samples - 1)
        consumed = 0
        for chunk in self.chunks:
            if sample_index < consumed + chunk.sample_count:
                return sample_index, chunk.sequence, sample_index - consumed
            consumed += chunk.sample_count
        raise AssertionError("unreachable: half-open boundary check failed")


def frozen_snapshot(report: Report) -> dict[str, Any]:
    snapshot = (report.result or {}).get("fixed_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("expected_chunks"):
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            f"report {report.id} does not carry a frozen manifest snapshot",
        )
    return snapshot


def frozen_segments(report: Report) -> list[FrozenSegment]:
    """Constant-rate segments built from the report's frozen manifest.

    This mirrors dsp.group_chunks_by_rate's segmentation rule (a rate change
    starts a new segment; order equals sequence order), but reads only frozen
    metadata, never raw samples. Equal display seconds in two rate segments
    therefore always resolve through their own block ranges.
    """
    snapshot = frozen_snapshot(report)
    ordered = sorted(snapshot["expected_chunks"], key=lambda item: int(item["sequence"]))
    specs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for item in ordered:
        rate = float(item["sample_rate"])
        chunk = FrozenChunk(
            sequence=int(item["sequence"]),
            sha256=str(item["sha256"]),
            # Keep the same key shape used at upload time; resolution also
            # cross-checks the live Chunk row's actual object_key.
            object_key=f"raw/{snapshot['manifest_id']}/{int(item['sequence']):09d}/{item['sha256']}.bin",
            sample_count=int(item["sample_count"]),
            sample_rate=rate,
            channels=list(item["channels"]),
            start_time=str(item["start_time"]),
            end_time=str(item["end_time"]),
        )
        if current is None or not math.isclose(current["sample_rate"], rate, rel_tol=0.0):
            current = {"sample_rate": rate, "channels": chunk.channels, "chunks": [], "total_samples": 0}
            specs.append(current)
        elif chunk.channels != current["channels"]:
            raise BookmarkError(
                409,
                "bookmark_source_stale",
                f"channel order changed at sequence {chunk.sequence} inside frozen manifest",
            )
        current["chunks"].append(chunk)
        current["total_samples"] += chunk.sample_count
    return [
        FrozenSegment(
            index=index,
            sample_rate=spec["sample_rate"],
            channels=spec["channels"],
            chunks=spec["chunks"],
            total_samples=spec["total_samples"],
        )
        for index, spec in enumerate(specs)
    ]


def get_report(db: Session, report_id: str) -> Report:
    report = db.get(Report, report_id)
    if report is None:
        raise BookmarkError(404, "report_not_found", f"report {report_id} not found")
    return report


def _check_source(db: Session, report: Report, segment: FrozenSegment, chunk: FrozenChunk) -> Chunk:
    """Verify the immutable block behind a bookmark is still resolvable.

    Raises BookmarkError(409, bookmark_source_stale) on any mismatch; never
    falls back to a neighbouring block or a different sample-rate segment.
    """
    manifest = db.get(Manifest, report.manifest_id)
    if manifest is None:
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            f"manifest {report.manifest_id} bound to report {report.id} no longer exists",
            {"manifest_id": report.manifest_id},
        )
    frozen_digest = frozen_snapshot(report).get("manifest_digest")
    if frozen_digest and manifest.manifest_digest != frozen_digest:
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            "manifest digest differs from the digest frozen in the report",
            {"frozen_digest": frozen_digest, "current_digest": manifest.manifest_digest},
        )
    live = db.scalar(
        select(Chunk).where(Chunk.manifest_id == report.manifest_id, Chunk.sequence == chunk.sequence).limit(1)
    )
    if live is None:
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            f"raw block sequence {chunk.sequence} is no longer present",
            {"chunk_sequence": chunk.sequence},
        )
    if live.sha256 != chunk.sha256:
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            f"raw block sequence {chunk.sequence} digest no longer matches the frozen manifest",
            {"chunk_sequence": chunk.sequence, "frozen_sha256": chunk.sha256, "current_sha256": live.sha256},
        )
    if not get_object_store().exists(live.object_key):
        raise BookmarkError(
            409,
            "bookmark_source_stale",
            f"immutable object {live.object_key} for raw block {chunk.sequence} is missing from storage",
            {"chunk_sequence": chunk.sequence, "object_key": live.object_key},
        )
    return live


def create_bookmark(
    db: Session,
    *,
    report: Report,
    segment_index: int,
    channel: str,
    offset_seconds: float,
    bookmark_type: str,
    note: str,
    author: str,
) -> ReportBookmark:
    from .schemas import BOOKMARK_TYPES

    if bookmark_type not in BOOKMARK_TYPES:
        raise BookmarkError(
            422,
            "invalid_bookmark_type",
            f"bookmark_type must be one of {sorted(BOOKMARK_TYPES)}, got {bookmark_type!r}",
        )
    segments = frozen_segments(report)
    if not 0 <= segment_index < len(segments):
        raise BookmarkError(
            422,
            "bookmark_segment_not_found",
            f"segment_index {segment_index} does not exist; frozen report has {len(segments)} segment(s)",
            {"segment_index": segment_index, "segment_count": len(segments)},
        )
    segment = segments[segment_index]
    if channel not in segment.channels:
        raise BookmarkError(
            422,
            "bookmark_channel_not_found",
            f"channel {channel!r} is not part of segment {segment_index}; channels: {segment.channels}",
            {"segment_index": segment_index, "channel": channel, "segment_channels": segment.channels},
        )
    sample_index, chunk_sequence, chunk_sample_offset = segment.locate(offset_seconds)
    frozen_chunk = next(item for item in segment.chunks if item.sequence == chunk_sequence)
    live = _check_source(db, report, segment, frozen_chunk)

    bookmark = ReportBookmark(
        report_id=report.id,
        manifest_id=report.manifest_id,
        manifest_digest=str(frozen_snapshot(report).get("manifest_digest") or ""),
        segment_index=segment_index,
        sample_rate=segment.sample_rate,
        channel=channel,
        offset_seconds=offset_seconds,
        sample_index=sample_index,
        chunk_sequence=chunk_sequence,
        chunk_sample_offset=chunk_sample_offset,
        chunk_sha256=frozen_chunk.sha256,
        object_key=live.object_key,
        bookmark_type=bookmark_type,
        note=note,
        author=author,
    )
    db.add(bookmark)
    db.flush()
    return bookmark


def resolve_bookmark(db: Session, bookmark: ReportBookmark) -> dict[str, Any]:
    report = get_report(db, bookmark.report_id)
    segments = frozen_segments(report)
    stale_code: str | None = None
    stale_message: str | None = None
    chunk_meta: FrozenChunk | None = None
    live_key: str = bookmark.object_key

    if not 0 <= bookmark.segment_index < len(segments):
        stale_code = "bookmark_source_stale"
        stale_message = (
            f"frozen report no longer contains segment {bookmark.segment_index}; "
            f"it now has {len(segments)} segment(s)"
        )
        segment = None
    else:
        segment = segments[bookmark.segment_index]
        chunk_meta = next((item for item in segment.chunks if item.sequence == bookmark.chunk_sequence), None)
        if chunk_meta is None:
            stale_code = "bookmark_source_stale"
            stale_message = f"raw block sequence {bookmark.chunk_sequence} is not part of frozen segment {bookmark.segment_index}"
        elif not math.isclose(segment.sample_rate, bookmark.sample_rate, rel_tol=0.0):
            stale_code = "bookmark_source_stale"
            stale_message = (
                f"segment {bookmark.segment_index} sample rate changed from "
                f"{bookmark.sample_rate:g} Hz to {segment.sample_rate:g} Hz"
            )
        elif bookmark.channel not in segment.channels:
            stale_code = "bookmark_source_stale"
            stale_message = f"channel {bookmark.channel!r} is no longer part of frozen segment {bookmark.segment_index}"

    if stale_code is None and segment is not None and chunk_meta is not None:
        try:
            live = _check_source(db, report, segment, chunk_meta)
            live_key = live.object_key
        except BookmarkError as exc:
            stale_code = exc.code
            stale_message = str(exc)

    return {
        "bookmark": bookmark,
        "manifest_id": report.manifest_id,
        "segment_index": bookmark.segment_index,
        "sample_rate": bookmark.sample_rate,
        "segment_start_seconds": 0.0,
        "segment_end_seconds": segment.duration_seconds if segment is not None else None,
        "offset_seconds": bookmark.offset_seconds,
        "channel": bookmark.channel,
        "chunk_sequence": bookmark.chunk_sequence,
        "chunk_sample_offset": bookmark.chunk_sample_offset,
        "chunk_sha256": bookmark.chunk_sha256,
        "chunk_start_time": chunk_meta.start_time if chunk_meta else None,
        "chunk_end_time": chunk_meta.end_time if chunk_meta else None,
        "object_key": live_key,
        "stale": stale_code is not None,
        "error_code": stale_code,
        "error_message": stale_message,
    }
