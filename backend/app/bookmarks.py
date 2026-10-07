from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Chunk, Manifest, Report, WaveformBookmark

# Explicit error codes surfaced to reviewers:
#   bookmark_segment_invalid      422 segment_index outside the frozen segmentation
#   bookmark_sample_rate_mismatch 422 caller's view of the segment rate disagrees with the frozen layout
#   bookmark_channel_invalid      422 channel not present in the frozen segment
#   bookmark_time_out_of_range    422 offset_seconds outside [0, segment_duration]
#   bookmark_source_invalid       409 report/manifest/chunk the bookmark traces to is gone or changed
#   snapshot_missing              422 report carries no frozen snapshot to anchor against


class BookmarkError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def segment_layout(expected_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Constant-rate segmentation over chunk metadata alone.

    Mirrors dsp.group_chunks_by_rate: sequence order, exact float rate equality,
    a rate change opens a new segment, samples are never resampled. Display
    seconds follow the preview convention: each segment spans
    total_samples / sample_rate seconds and segments are laid back to back.
    """

    ordered = sorted(expected_chunks, key=lambda item: int(item["sequence"]))
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for meta in ordered:
        rate = float(meta["sample_rate"])
        sequence = int(meta["sequence"])
        if current is None or not math.isclose(rate, current["sample_rate"], rel_tol=0.0):
            current = {
                "sample_rate": rate,
                "channels": list(meta["channels"]),
                "total_samples": 0,
                "start_sequence": sequence,
                "end_sequence": sequence,
                "chunks": [],
            }
            segments.append(current)
        sample_count = int(meta["sample_count"])
        current["chunks"].append(
            {
                "sequence": sequence,
                "sample_count": sample_count,
                "start_sample": current["total_samples"],
            }
        )
        current["total_samples"] += sample_count
        current["end_sequence"] = sequence

    cursor = 0.0
    for index, segment in enumerate(segments):
        segment["index"] = index
        segment["duration_seconds"] = segment["total_samples"] / segment["sample_rate"]
        segment["display_start"] = cursor
        cursor += segment["duration_seconds"]
    return segments


def frozen_layout(report: Report) -> list[dict[str, Any]]:
    snapshot = (report.result or {}).get("fixed_snapshot") or {}
    expected = snapshot.get("expected_chunks")
    if not expected:
        raise BookmarkError(
            "snapshot_missing",
            "report has no frozen manifest snapshot; bookmarks cannot be anchored",
        )
    return segment_layout(expected)


def anchor_point(segment: dict[str, Any], offset_seconds: float) -> tuple[int, int]:
    """Map a within-segment offset to (chunk_sequence, sample_index_in_chunk)."""

    duration = segment["duration_seconds"]
    if not math.isfinite(offset_seconds) or offset_seconds < 0.0 or offset_seconds > duration:
        raise BookmarkError(
            "bookmark_time_out_of_range",
            f"offset {offset_seconds}s is outside segment #{segment['index']} duration [0, {duration}]s",
            offset_seconds=offset_seconds if math.isfinite(offset_seconds) else str(offset_seconds),
            segment_index=segment["index"],
            segment_duration_seconds=duration,
        )
    # The preview axis extends one sample period past the last sample; a click
    # exactly at the segment end clamps to the final sample of the final chunk.
    position = int(round(offset_seconds * segment["sample_rate"]))
    position = max(0, min(position, segment["total_samples"] - 1))
    for chunk in segment["chunks"]:
        if position < chunk["start_sample"] + chunk["sample_count"]:
            return chunk["sequence"], position - chunk["start_sample"]
    last = segment["chunks"][-1]
    return last["sequence"], last["sample_count"] - 1


def ensure_source_valid(db: Session, bookmark: WaveformBookmark) -> dict[str, Any]:
    """Prove the bookmark can still trace back to its raw block, or fail loudly."""

    report = db.get(Report, bookmark.report_id)
    if report is None:
        raise BookmarkError(
            "bookmark_source_invalid",
            "the report this bookmark belongs to no longer exists",
            409,
            reason="report_missing",
        )
    manifest = db.get(Manifest, bookmark.manifest_id)
    if manifest is None:
        raise BookmarkError(
            "bookmark_source_invalid",
            "the manifest behind this bookmark no longer exists; it cannot be traced to raw data",
            409,
            reason="manifest_missing",
        )
    if manifest.manifest_digest != bookmark.manifest_digest:
        raise BookmarkError(
            "bookmark_source_invalid",
            "manifest digest differs from the digest bound at bookmark creation",
            409,
            reason="manifest_digest_changed",
            bound_digest=bookmark.manifest_digest,
            current_digest=manifest.manifest_digest,
        )
    chunk = db.scalar(
        select(Chunk)
        .where(Chunk.manifest_id == bookmark.manifest_id, Chunk.sequence == bookmark.chunk_sequence)
        .limit(1)
    )
    if chunk is None:
        raise BookmarkError(
            "bookmark_source_invalid",
            f"raw chunk {bookmark.chunk_sequence} is missing from the manifest",
            409,
            reason="chunk_missing",
            chunk_sequence=bookmark.chunk_sequence,
        )
    snapshot = (report.result or {}).get("fixed_snapshot") or {}
    frozen = {
        int(item["sequence"]): item for item in snapshot.get("expected_chunks", [])
    }.get(bookmark.chunk_sequence)
    if frozen is not None and chunk.sha256 != frozen["sha256"]:
        raise BookmarkError(
            "bookmark_source_invalid",
            f"raw chunk {bookmark.chunk_sequence} digest differs from the frozen snapshot",
            409,
            reason="chunk_digest_changed",
            chunk_sequence=bookmark.chunk_sequence,
        )
    return {"report": report, "manifest": manifest, "chunk": chunk}


def create_bookmark(
    db: Session,
    *,
    report: Report,
    segment_index: int,
    channel: str,
    offset_seconds: float,
    sample_rate: float | None,
    bookmark_type: str,
    note: str,
    author: str,
) -> WaveformBookmark:
    layout = frozen_layout(report)
    if segment_index < 0 or segment_index >= len(layout):
        raise BookmarkError(
            "bookmark_segment_invalid",
            f"segment_index {segment_index} is outside the frozen segmentation of {len(layout)} segment(s)",
            segment_index=segment_index,
            segment_count=len(layout),
        )
    segment = layout[segment_index]
    if sample_rate is not None and not math.isclose(float(sample_rate), segment["sample_rate"], rel_tol=0.0):
        raise BookmarkError(
            "bookmark_sample_rate_mismatch",
            f"caller sees {sample_rate} Hz but frozen segment #{segment_index} is {segment['sample_rate']} Hz; "
            "refusing to anchor against a stale view",
            segment_index=segment_index,
            expected_sample_rate=segment["sample_rate"],
            provided_sample_rate=float(sample_rate),
        )
    if channel not in segment["channels"]:
        raise BookmarkError(
            "bookmark_channel_invalid",
            f"channel {channel!r} is not part of frozen segment #{segment_index}",
            channel=channel,
            segment_channels=segment["channels"],
        )
    chunk_sequence, sample_index = anchor_point(segment, float(offset_seconds))
    snapshot = report.result["fixed_snapshot"]
    bookmark = WaveformBookmark(
        report_id=report.id,
        manifest_id=snapshot["manifest_id"],
        manifest_digest=snapshot["manifest_digest"],
        segment_index=segment_index,
        sample_rate=segment["sample_rate"],
        channel=channel,
        offset_seconds=float(offset_seconds),
        display_seconds=segment["display_start"] + float(offset_seconds),
        chunk_sequence=chunk_sequence,
        sample_index=sample_index,
        bookmark_type=bookmark_type,
        note=note,
        author=author,
    )
    # A bookmark on an already-invalid source is rejected, never stored.
    ensure_source_valid(db, bookmark)
    db.add(bookmark)
    db.flush()
    return bookmark


def locate_bookmark(db: Session, bookmark: WaveformBookmark) -> dict[str, Any]:
    """Resolve a persisted bookmark back to its preview window and raw block."""

    found = ensure_source_valid(db, bookmark)
    layout = frozen_layout(found["report"])
    if bookmark.segment_index >= len(layout):
        raise BookmarkError(
            "bookmark_source_invalid",
            f"frozen segment #{bookmark.segment_index} no longer exists in the report snapshot",
            409,
            reason="segment_missing",
        )
    segment = layout[bookmark.segment_index]
    return {
        "bookmark_id": bookmark.id,
        "report_id": bookmark.report_id,
        "manifest_id": bookmark.manifest_id,
        "manifest_digest": bookmark.manifest_digest,
        "segment_index": bookmark.segment_index,
        "sample_rate": bookmark.sample_rate,
        "channel": bookmark.channel,
        "offset_seconds": bookmark.offset_seconds,
        "display_seconds": segment["display_start"] + bookmark.offset_seconds,
        "segment_display_start": segment["display_start"],
        "segment_display_end": segment["display_start"] + segment["duration_seconds"],
        "chunk_sequence": bookmark.chunk_sequence,
        "sample_index": bookmark.sample_index,
        "chunk_object_key": found["chunk"].object_key,
        "chunk_sha256": found["chunk"].sha256,
    }
