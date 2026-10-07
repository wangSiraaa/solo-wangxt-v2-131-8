import hashlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database import SessionLocal
from app.models import Chunk, Manifest, Report, WaveformBookmark
from app.storage import get_object_store
from tests.synthetic import (
    create_and_run_task,
    create_manifest,
    make_calibration,
    make_multi_rate_chunks,
    upload_chunks,
)

# Layout of the two-segment recording used throughout this module:
#   segment 0: chunk 0, 300 samples @ 6000 Hz -> 0.05 s, display [0.00, 0.05]
#   segment 1: chunks 1+2, 700 samples @ 7000 Hz -> 0.10 s, display [0.05, 0.15]
SPECS = [(300, 6000.0), (350, 7000.0), (350, 7000.0)]


@pytest.fixture()
def rate_change_report(client: TestClient):
    chunks = make_multi_rate_chunks(SPECS)
    manifest = create_manifest(client, chunks, name="two-rates")
    upload_chunks(client, manifest["id"], chunks)
    client.post(f"/manifests/{manifest['id']}/finalize")
    calibration = make_calibration(client, manifest["channel_set_hash"])
    task_id = create_and_run_task(client, manifest["id"], calibration["id"])
    report = client.get(f"/reports?manifest_id={manifest['id']}").json()[0]
    assert report["status"] == "published"
    return {"manifest": manifest, "report": report, "task_id": task_id}


def add_bookmark(client, report_id, **overrides):
    payload = {
        "segment_index": 0,
        "channel": "Va",
        "offset_seconds": 0.01,
        "bookmark_type": "anomaly",
        "note": "短时异常",
        "author": "reviewer-1",
    }
    payload.update(overrides)
    return client.post(f"/reports/{report_id}/bookmarks", json=payload)


def test_same_display_second_in_two_rate_segments_resolves_to_correct_chunks(client: TestClient, rate_change_report):
    report_id = rate_change_report["report"]["id"]

    # Segment 0 ends exactly at display 0.05 s; segment 1 starts at display 0.05 s.
    at_end_of_first = add_bookmark(client, report_id, segment_index=0, offset_seconds=0.05, sample_rate=6000.0)
    assert at_end_of_first.status_code == 201, at_end_of_first.text
    at_start_of_second = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.0, sample_rate=7000.0)
    assert at_start_of_second.status_code == 201, at_start_of_second.text

    first = at_end_of_first.json()
    second = at_start_of_second.json()
    # Same display second, different physical locations: no cross-wiring.
    assert abs(first["display_seconds"] - second["display_seconds"]) < 1e-12
    assert (first["chunk_sequence"], first["sample_index"]) == (0, 299)
    assert (second["chunk_sequence"], second["sample_index"]) == (1, 0)

    # Same within-segment offset at different rates maps to different samples/chunks.
    slow = add_bookmark(client, report_id, segment_index=0, offset_seconds=0.01).json()
    fast = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.01).json()
    assert (slow["chunk_sequence"], slow["sample_index"]) == (0, 60)
    assert (fast["chunk_sequence"], fast["sample_index"]) == (1, 70)
    assert abs(fast["display_seconds"] - 0.06) < 1e-12

    # Offset landing exactly on the chunk boundary inside segment 1 belongs to chunk 2.
    boundary = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.05).json()
    assert (boundary["chunk_sequence"], boundary["sample_index"]) == (2, 0)
    assert abs(boundary["display_seconds"] - 0.10) < 1e-12


def test_bookmark_locate_is_stable_across_requests(client: TestClient, rate_change_report):
    report_id = rate_change_report["report"]["id"]
    bookmark = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.01).json()

    first = client.get(f"/waveform-bookmarks/{bookmark['id']}/locate")
    second = client.get(f"/waveform-bookmarks/{bookmark['id']}/locate")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    located = first.json()
    assert located["chunk_sequence"] == 1
    assert located["sample_index"] == 70
    assert located["segment_index"] == 1
    assert located["sample_rate"] == 7000.0
    assert abs(located["display_seconds"] - 0.06) < 1e-12
    assert located["chunk_object_key"].startswith(f"raw/{rate_change_report['manifest']['id']}/")
    assert located["chunk_sha256"] == hashlib.sha256(
        get_object_store().get_bytes(located["chunk_object_key"])
    ).hexdigest()


def test_bookmark_binds_report_manifest_digest_and_chunk(client: TestClient, rate_change_report):
    manifest = rate_change_report["manifest"]
    report_id = rate_change_report["report"]["id"]
    response = add_bookmark(
        client,
        report_id,
        segment_index=1,
        channel="Vc",
        offset_seconds=0.02,
        bookmark_type="question",
        note="疑似暂降",
        author="reviewer-9",
    )
    assert response.status_code == 201, response.text
    bookmark = response.json()
    assert bookmark["report_id"] == report_id
    assert bookmark["manifest_id"] == manifest["id"]
    assert bookmark["manifest_digest"] == manifest["manifest_digest"]
    assert bookmark["segment_index"] == 1
    assert bookmark["sample_rate"] == 7000.0
    assert bookmark["channel"] == "Vc"
    assert bookmark["chunk_sequence"] == 1
    assert bookmark["sample_index"] == 140
    assert bookmark["bookmark_type"] == "question"
    assert bookmark["note"] == "疑似暂降"
    assert bookmark["author"] == "reviewer-9"
    assert bookmark["source_valid"] is True

    listed = client.get(f"/reports/{report_id}/bookmarks").json()
    assert [item["id"] for item in listed] == [bookmark["id"]]
    assert listed[0]["source_valid"] is True


def test_out_of_range_time_and_invalid_selections_are_explicit_errors(client: TestClient, rate_change_report):
    report_id = rate_change_report["report"]["id"]

    beyond = add_bookmark(client, report_id, segment_index=0, offset_seconds=0.0501)
    assert beyond.status_code == 422
    assert beyond.json()["detail"]["code"] == "bookmark_time_out_of_range"

    negative = add_bookmark(client, report_id, segment_index=0, offset_seconds=-0.5)
    assert negative.status_code == 422
    assert negative.json()["detail"]["code"] == "bookmark_time_out_of_range"

    for bad_value in ("NaN", "Infinity", "-Infinity"):
        non_finite = client.post(
            f"/reports/{report_id}/bookmarks",
            content='{"segment_index": 0, "channel": "Va", "offset_seconds": %s, "author": "x"}' % bad_value,
            headers={"Content-Type": "application/json"},
        )
        assert non_finite.status_code == 422, bad_value

    bad_segment = add_bookmark(client, report_id, segment_index=2, offset_seconds=0.01)
    assert bad_segment.status_code == 422
    assert bad_segment.json()["detail"]["code"] == "bookmark_segment_invalid"

    bad_channel = add_bookmark(client, report_id, segment_index=0, channel="Vd", offset_seconds=0.01)
    assert bad_channel.status_code == 422
    assert bad_channel.json()["detail"]["code"] == "bookmark_channel_invalid"

    stale_rate = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.01, sample_rate=6000.0)
    assert stale_rate.status_code == 422
    assert stale_rate.json()["detail"]["code"] == "bookmark_sample_rate_mismatch"

    missing_report = client.post(
        "/reports/does-not-exist/bookmarks",
        json={"segment_index": 0, "channel": "Va", "offset_seconds": 0.01, "author": "x"},
    )
    assert missing_report.status_code == 404
    assert missing_report.json()["detail"]["code"] == "report_not_found"

    with SessionLocal() as db:
        assert db.query(WaveformBookmark).count() == 0


def test_invalidated_source_is_an_explicit_error(client: TestClient, rate_change_report):
    manifest_id = rate_change_report["manifest"]["id"]
    report_id = rate_change_report["report"]["id"]
    bookmark = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.01).json()

    with SessionLocal() as db:
        manifest = db.get(Manifest, manifest_id)
        manifest.manifest_digest = "0" * 64
        db.commit()
    changed = client.get(f"/waveform-bookmarks/{bookmark['id']}/locate")
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "bookmark_source_invalid"
    assert changed.json()["detail"]["reason"] == "manifest_digest_changed"

    with SessionLocal() as db:
        manifest = db.get(Manifest, manifest_id)
        manifest.manifest_digest = rate_change_report["manifest"]["manifest_digest"]
        chunk = db.scalar(db.query(Chunk).filter_by(manifest_id=manifest_id, sequence=1).limit(1).statement)
        db.delete(chunk)
        db.commit()
    missing_chunk = client.get(f"/waveform-bookmarks/{bookmark['id']}/locate")
    assert missing_chunk.status_code == 409
    assert missing_chunk.json()["detail"]["reason"] == "chunk_missing"

    with SessionLocal() as db:
        # Core delete simulates source loss without ORM nullify cascades.
        db.execute(delete(Manifest).where(Manifest.id == manifest_id))
        db.commit()
    gone = client.get(f"/waveform-bookmarks/{bookmark['id']}/locate")
    assert gone.status_code == 409
    assert gone.json()["detail"]["code"] == "bookmark_source_invalid"
    assert gone.json()["detail"]["reason"] == "manifest_missing"

    # The list marks the bookmark invalid instead of silently dropping it,
    # and creating new bookmarks against the dead source is refused.
    listed = client.get(f"/reports/{report_id}/bookmarks").json()
    assert listed[0]["source_valid"] is False
    assert listed[0]["source_error"]["code"] == "bookmark_source_invalid"
    refused = add_bookmark(client, report_id, segment_index=0, offset_seconds=0.01)
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "bookmark_source_invalid"


def test_bookmarks_do_not_mutate_report_chunks_metrics_or_quality(client: TestClient, rate_change_report):
    manifest_id = rate_change_report["manifest"]["id"]
    report_id = rate_change_report["report"]["id"]
    report_before = client.get(f"/reports/{report_id}").json()
    chunks_before = client.get(f"/manifests/{manifest_id}/chunks").json()
    store = get_object_store()
    raw_before = {chunk["object_key"]: store.get_bytes(chunk["object_key"]) for chunk in chunks_before}

    first = add_bookmark(client, report_id, segment_index=0, offset_seconds=0.02, note="A").json()
    second = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.03, note="B").json()
    client.get(f"/reports/{report_id}/bookmarks")
    client.get(f"/waveform-bookmarks/{first['id']}/locate")
    client.delete(f"/waveform-bookmarks/{second['id']}")

    report_after = client.get(f"/reports/{report_id}").json()
    assert report_after["result"] == report_before["result"]
    assert report_after["status"] == report_before["status"]
    assert report_after["result"]["quality_status"] == report_before["result"]["quality_status"]
    assert client.get(f"/manifests/{manifest_id}/chunks").json() == chunks_before
    for key, data in raw_before.items():
        assert store.get_bytes(key) == data


def test_delete_bookmark_keeps_report_and_raw_recording(client: TestClient, rate_change_report):
    manifest_id = rate_change_report["manifest"]["id"]
    report_id = rate_change_report["report"]["id"]
    bookmark = add_bookmark(client, report_id, segment_index=1, offset_seconds=0.01).json()

    deleted = client.delete(f"/waveform-bookmarks/{bookmark['id']}")
    assert deleted.status_code == 204
    assert client.get(f"/waveform-bookmarks/{bookmark['id']}/locate").status_code == 404
    assert client.get(f"/reports/{report_id}/bookmarks").json() == []

    report = client.get(f"/reports/{report_id}")
    assert report.status_code == 200
    assert report.json()["status"] == "published"
    assert len(client.get(f"/manifests/{manifest_id}/chunks").json()) == len(SPECS)
    preview = client.get(f"/manifests/{manifest_id}/preview")
    assert preview.status_code == 200
    assert [segment["sample_rate"] for segment in preview.json()["segments"]] == [6000.0, 7000.0]
    with SessionLocal() as db:
        assert db.get(Report, report_id) is not None
        assert db.query(Chunk).filter_by(manifest_id=manifest_id).count() == len(SPECS)

    again = client.delete(f"/waveform-bookmarks/{bookmark['id']}")
    assert again.status_code == 404
    assert again.json()["detail"]["code"] == "bookmark_not_found"
