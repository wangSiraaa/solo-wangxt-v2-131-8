from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExpectedChunkIn(BaseModel):
    sequence: int = Field(ge=0)
    sha256: str = Field(min_length=64, max_length=64)
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    sample_rate: float = Field(gt=0)
    channels: list[str]
    start_time: str
    end_time: str
    encoding: str = "float32le-interleaved"


class ManifestCreate(BaseModel):
    name: str
    nominal_sample_rate: float | None = None
    expected_chunks: list[ExpectedChunkIn]
    start_time: str | None = None
    end_time: str | None = None


class ManifestOut(BaseModel):
    id: str
    name: str
    status: str
    expected_chunks: list[dict[str, Any]]
    channel_set: list[str]
    channel_set_hash: str
    nominal_sample_rate: float | None
    start_time: str | None
    end_time: str | None
    manifest_digest: str | None
    error: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class IssueOut(BaseModel):
    id: str
    severity: str
    code: str
    message: str
    details: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ChunkOut(BaseModel):
    id: str
    sequence: int
    object_key: str
    sha256: str
    byte_offset: int
    byte_length: int
    sample_count: int
    sample_rate: float
    channels: list[str]
    start_time: str
    end_time: str
    encoding: str
    received_at: datetime

    model_config = {"from_attributes": True}


class CalibrationCoefficient(BaseModel):
    gain: float = 1.0
    offset: float = 0.0
    phase_shift_rad: float = 0.0
    saturation_low: float | None = None
    saturation_high: float | None = None


class CalibrationCreate(BaseModel):
    channel_set_hash: str
    coefficients: dict[str, CalibrationCoefficient]
    change_note: str | None = None
    created_by: str = "lab"
    mark_previous_for_review: bool = True


class CalibrationOut(BaseModel):
    id: str
    channel_set_hash: str
    status: str
    coefficients: dict[str, Any]
    change_note: str | None
    supersedes_id: str | None
    created_by: str
    created_at: datetime
    activated_at: datetime

    model_config = {"from_attributes": True}


class AnalysisCreate(BaseModel):
    manifest_id: str
    calibration_version_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TaskOut(BaseModel):
    id: str
    manifest_id: str
    calibration_version_id: str
    status: str
    params: dict[str, Any]
    manifest_snapshot: dict[str, Any]
    stage_results: dict[str, Any]
    attempts: int
    lease_owner: str | None
    lease_until: datetime | None
    heartbeat_at: datetime | None
    error_code: str | None
    error_message: str | None
    cancellation_requested: bool
    idempotency_key: str | None
    requested_at: datetime
    started_at: datetime | None
    ended_at: datetime | None

    model_config = {"from_attributes": True}


class ReportOut(BaseModel):
    id: str
    task_id: str
    manifest_id: str
    calibration_version_id: str
    status: str
    result: dict[str, Any]
    snapshot_digest: str
    published_at: datetime | None
    review_reason: str | None
    superseded_by_calibration_id: str | None

    model_config = {"from_attributes": True}


class RetryOut(BaseModel):
    task_id: str
    status: str
    attempts: int


BOOKMARK_TYPES = ("anomaly", "question", "note")


class BookmarkCreate(BaseModel):
    segment_index: int = Field(ge=0)
    channel: str = Field(min_length=1, max_length=128)
    # Seconds measured from the start of the constant-rate segment (the same
    # local time axis shown by the preview); never a global recording time.
    offset_seconds: float = Field(ge=0.0)
    bookmark_type: str
    note: str = Field(min_length=1, max_length=4000)
    author: str = Field(min_length=1, max_length=128)


class BookmarkOut(BaseModel):
    id: str
    report_id: str
    manifest_id: str
    manifest_digest: str
    segment_index: int
    sample_rate: float
    channel: str
    offset_seconds: float
    sample_index: int
    chunk_sequence: int
    chunk_sample_offset: int
    chunk_sha256: str
    object_key: str
    bookmark_type: str
    note: str
    author: str
    created_at: datetime

    model_config = {"from_attributes": True}


class BookmarkResolutionOut(BaseModel):
    bookmark: BookmarkOut
    manifest_id: str
    segment_index: int
    sample_rate: float
    segment_start_seconds: float
    segment_end_seconds: float
    offset_seconds: float
    channel: str
    chunk_sequence: int
    chunk_sample_offset: int
    chunk_sha256: str
    chunk_start_time: str
    chunk_end_time: str
    stale: bool
    error_code: str | None = None
    error_message: str | None = None


QualitySeverity = Literal["ok", "warning", "error"]
