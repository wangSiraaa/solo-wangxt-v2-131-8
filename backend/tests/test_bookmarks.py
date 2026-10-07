import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.models import Chunk, Report, ReportBookmark
from app.storage import get_object_store
from tests.synthetic import (
    create_and_run_task,
    create_manifest,
    make_calibration,
    make_rate_change_chunks,
    upload_chunks,
)


def completed_rate_change_recording(client):
    chunks = make_rate_change_chunks()
    manifest = create_manifest(client, chunks, name="rate-change", nominal_sample_rate=6000.0)
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    return manifest, calibration, report, task_id


def completed_simple_recording(client, sample_chunks=(300, 420), fs=6000.0):
    from tests.synthetic import make_chunks

    chunks = make_chunks(sample_chunks=sample_chunks, fs=fs)
    manifest = create_manifest(client, chunks, name="simple")
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    create_and_run_task(client, manifest["id"], calibration["id"])
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    return manifest, report, chunks


def add_bookmark(client, report_id, **overrides):
    payload = {
        "segment_index": 0,
        "channel": "Va",
        "offset_seconds": 0.01,
        "bookmark_type": "anomaly",
        "note": "短时异常：疑似脉冲",
        "author": "reviewer-zhao",
    }
    payload.update(overrides)
    return client.post(f"/reports/{report_id}/bookmarks", json=payload)


def test_bookmark_binds_report_manifest_summary_and_raw_block(client: TestClient):
    manifest, report, chunks = completed_simple_recording(client)
    response = add_bookmark(client, report["id"])
    assert response.status_code == 201, response.text
    bookmark = response.json()
    assert bookmark["report_id"] == report["id"]
    assert bookmark["manifest_id"] == manifest["id"]
    assert bookmark["manifest_digest"] == manifest["manifest_digest"]
    assert bookmark["sample_rate"] == 6000.0
    assert bookmark["channel"] == "Va"
    assert bookmark["bookmark_type"] == "anomaly"
    assert bookmark["author"] == "reviewer-zhao"
    # 0.01 s * 6000 Hz = sample 60 -> chunk 0 holds 300 samples -> sequence 0.
    assert bookmark["sample_index"] == 60
    assert bookmark["chunk_sequence"] == 0
    assert bookmark["chunk_sample_offset"] == 60
    assert bookmark["chunk_sha256"] == chunks[0]["sha256"]
    assert bookmark["object_key"].endswith(f"/{chunks[0]['sha256']}.bin")


def test_same_displayed_second_in_two_rate_segments_resolves_to_different_blocks(client: TestClient):
    manifest, calibration, report, task_id = completed_rate_change_recording(client)
    preview = client.get(f"/manifests/{manifest['id']}/preview").json()
    assert len(preview["segments"]) == 2
    # Both local axes start at 0, so 0.02s exists on both; rates/chunks differ.
    assert preview["segments"][0]["start_seconds"] == preview["segments"][1]["start_seconds"] == 0.0
    assert preview["segments"][0]["sample_rate"] == 6000.0
    assert preview["segments"][1]["sample_rate"] == 7000.0

    first = add_bookmark(client, report["id"], segment_index=0, offset_seconds=0.02).json()
    second = add_bookmark(client, report["id"], segment_index=1, offset_seconds=0.02).json()
    # 0.02*6000=120 -> still inside chunk 0 (300 samples); 0.02*7000=140 -> chunk 1.
    assert first["sample_rate"] == 6000.0
    assert first["sample_index"] == 120
    assert first["chunk_sequence"] == 0
    assert first["chunk_sample_offset"] == 120
    assert second["sample_rate"] == 7000.0
    assert second["sample_index"] == 140
    assert second["chunk_sequence"] == 1
    assert second["chunk_sample_offset"] == 140

    # Segment 0 is exactly chunk 0 (300 samples @6 kHz = 0.05 s): the exact
    # end instant belongs to no sample and must be rejected rather than
    # silently spilling into chunk 1 / the next rate segment.
    boundary = add_bookmark(client, report["id"], segment_index=0, offset_seconds=0.05)
    assert boundary.status_code == 422
    assert boundary.json()["detail"]["code"] == "bookmark_time_out_of_range"

    listed = {item["id"]: item for item in client.get(f"/reports/{report['id']}/bookmarks").json()}
    assert {first["id"], second["id"]} <= set(listed)


def test_offset_beyond_segment_duration_is_an_explicit_error(client: TestClient):
    manifest, calibration, report, _ = completed_rate_change_recording(client)
    response = add_bookmark(client, report["id"], segment_index=1, offset_seconds=1.0)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "bookmark_time_out_of_range"
    assert detail["segment_index"] == 1
    assert detail["segment_duration_seconds"] == pytest.approx(420 / 7000.0)

    # Negative and non-finite times are rejected at the schema boundary.
    bad = client.post(
        f"/reports/{report['id']}/bookmarks",
        json={**{
            "segment_index": 0, "channel": "Va", "bookmark_type": "note",
            "note": "x", "author": "a",
        }, "offset_seconds": -0.1},
    )
    assert bad.status_code == 422


def test_bad_segment_channel_and_type_are_explicit_errors(client: TestClient):
    manifest, calibration, report, _ = completed_rate_change_recording(client)
    missing_segment = add_bookmark(client, report["id"], segment_index=7, offset_seconds=0.01)
    assert missing_segment.status_code == 422
    assert missing_segment.json()["detail"]["code"] == "bookmark_segment_not_found"

    bad_channel = add_bookmark(client, report["id"], channel="Vx", offset_seconds=0.01)
    assert bad_channel.status_code == 422
    assert bad_channel.json()["detail"]["code"] == "bookmark_channel_not_found"

    bad_type = add_bookmark(client, report["id"], bookmark_type="secret")
    assert bad_type.status_code == 422
    assert bad_type.json()["detail"]["code"] == "invalid_bookmark_type"


def test_bookmark_on_unknown_report_is_404(client: TestClient):
    response = add_bookmark(client, "does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "report_not_found"


def test_resolution_survives_refresh_and_locates_preview_window(client: TestClient):
    manifest, calibration, report, _ = completed_rate_change_recording(client)
    created = add_bookmark(client, report["id"], segment_index=1, channel="Vc", offset_seconds=0.03).json()

    # Re-read through a brand-new DB session/HTTP request: persistence holds.
    resolution = client.get(f"/bookmarks/{created['id']}/resolution")
    assert resolution.status_code == 200, resolution.text
    data = resolution.json()
    assert data["stale"] is False
    assert data["error_code"] is None
    assert data["manifest_id"] == manifest["id"]
    assert data["segment_index"] == 1
    assert data["sample_rate"] == 7000.0
    assert data["segment_start_seconds"] == 0.0
    assert data["segment_end_seconds"] == pytest.approx(420 / 7000.0)
    assert data["offset_seconds"] == 0.03
    assert data["channel"] == "Vc"
    assert data["chunk_sequence"] == 1
    assert data["chunk_sample_offset"] == 210  # 0.03*7000
    assert data["chunk_sha256"] == created["chunk_sha256"]


def test_deleted_or_mismatched_raw_block_marks_bookmark_stale(client: TestClient):
    manifest, calibration, report, _ = completed_rate_change_recording(client)
    created = add_bookmark(client, report["id"], offset_seconds=0.02).json()

    with SessionLocal() as db:
        chunk = db.query(Chunk).filter_by(manifest_id=manifest["id"], sequence=0).one()
        digest = chunk.sha256
        chunk.sha256 = "0" * 64  # live block metadata no longer matches frozen digest
        db.commit()

    resolution = client.get(f"/bookmarks/{created['id']}/resolution")
    assert resolution.status_code == 409
    detail = resolution.json()["detail"]
    assert detail["code"] == "bookmark_source_stale"
    assert "digest" in detail["message"]

    with SessionLocal() as db:
        chunk = db.query(Chunk).filter_by(manifest_id=manifest["id"], sequence=0).one()
        chunk.sha256 = digest
        db.commit()

    # Missing immutable object is also an explicit stale error.
    store = get_object_store()
    with SessionLocal() as db:
        chunk = db.query(Chunk).filter_by(manifest_id=manifest["id"], sequence=0).one()
        key = chunk.object_key
    del store._objects[key]
    resolution = client.get(f"/bookmarks/{created['id']}/resolution")
    assert resolution.status_code == 409
    assert resolution.json()["detail"]["code"] == "bookmark_source_stale"

    # A stale source cannot be used to create a new bookmark either.
    response = add_bookmark(client, report["id"], offset_seconds=0.02)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "bookmark_source_stale"


def test_missing_raw_block_row_marks_bookmark_stale(client: TestClient):
    manifest, report, _ = completed_simple_recording(client)
    created = add_bookmark(client, report["id"], offset_seconds=0.02).json()

    with SessionLocal() as db:
        db.query(Chunk).filter_by(manifest_id=manifest["id"], sequence=0).delete()
        db.commit()

    resolution = client.get(f"/bookmarks/{created['id']}/resolution")
    assert resolution.status_code == 409
    assert resolution.json()["detail"]["code"] == "bookmark_source_stale"


def test_deleting_bookmark_keeps_report_and_raw_recording(client: TestClient):
    manifest, report, chunks = completed_simple_recording(client)
    created = add_bookmark(client, report["id"]).json()

    deleted = client.delete(f"/reports/{report['id']}/bookmarks/{created['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/reports/{report['id']}/bookmarks").json() == []
    assert client.get(f"/reports/{report['id']}").status_code == 200
    with SessionLocal() as db:
        assert db.get(Report, report["id"]) is not None
        assert db.query(Chunk).filter_by(manifest_id=manifest["id"]).count() == len(chunks)
        assert db.get(ReportBookmark, created["id"]) is None
    # Immutable raw bytes remain in the object store.
    assert get_object_store().exists(created["object_key"])

    # Deleting again or via another report is a 404.
    assert client.delete(f"/reports/{report['id']}/bookmarks/{created['id']}").status_code == 404


def test_bookmarks_never_change_metrics_or_quality_status(client: TestClient):
    manifest, report, _ = completed_simple_recording(client)
    before = client.get(f"/reports/{report['id']}").json()
    add_bookmark(client, report["id"], bookmark_type="question", note="RMS 看着不对", author="q")
    add_bookmark(client, report["id"], bookmark_type="note", note="仅记录", author="q", offset_seconds=0.04)
    after = client.get(f"/reports/{report['id']}").json()
    assert after["result"] == before["result"]
    assert after["status"] == before["status"]
    assert after["snapshot_digest"] == before["snapshot_digest"]
