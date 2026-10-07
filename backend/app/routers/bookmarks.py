from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import bookmarks as service
from ..database import get_db
from ..models import Report, ReportBookmark
from ..schemas import BookmarkCreate, BookmarkOut, BookmarkResolutionOut

router = APIRouter(tags=["bookmarks"])


def _raise(exc: service.BookmarkError) -> HTTPException:
    detail = {"code": exc.code, "message": str(exc)}
    detail.update(exc.details)
    return HTTPException(exc.status_code, detail)


@router.post("/reports/{report_id}/bookmarks", response_model=BookmarkOut, status_code=201)
def create_bookmark(report_id: str, payload: BookmarkCreate, db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, detail={"code": "report_not_found", "message": f"report {report_id} not found"})
    try:
        bookmark = service.create_bookmark(
            db,
            report=report,
            segment_index=payload.segment_index,
            channel=payload.channel,
            offset_seconds=payload.offset_seconds,
            bookmark_type=payload.bookmark_type,
            note=payload.note,
            author=payload.author,
        )
    except service.BookmarkError as exc:
        raise _raise(exc) from exc
    db.commit()
    db.refresh(bookmark)
    return bookmark


@router.get("/reports/{report_id}/bookmarks", response_model=list[BookmarkOut])
def list_bookmarks(report_id: str, db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, detail={"code": "report_not_found", "message": f"report {report_id} not found"})
    return db.scalars(
        select(ReportBookmark)
        .where(ReportBookmark.report_id == report_id)
        .order_by(ReportBookmark.created_at, ReportBookmark.segment_index, ReportBookmark.offset_seconds)
    ).all()


@router.delete("/reports/{report_id}/bookmarks/{bookmark_id}", status_code=204)
def delete_bookmark(report_id: str, bookmark_id: str, db: Session = Depends(get_db)):
    # Deleting an annotation only removes the bookmark row; reports, raw chunks
    # and immutable objects are never touched.
    bookmark = db.get(ReportBookmark, bookmark_id)
    if bookmark is None or bookmark.report_id != report_id:
        raise HTTPException(
            404, detail={"code": "bookmark_not_found", "message": f"bookmark {bookmark_id} not found on this report"}
        )
    db.delete(bookmark)
    db.commit()


@router.get("/bookmarks/{bookmark_id}/resolution", response_model=BookmarkResolutionOut)
def resolve_bookmark(bookmark_id: str, db: Session = Depends(get_db)):
    bookmark = db.get(ReportBookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(404, detail={"code": "bookmark_not_found", "message": "bookmark not found"})
    try:
        resolved = service.resolve_bookmark(db, bookmark)
    except service.BookmarkError as exc:
        raise _raise(exc) from exc
    if resolved["stale"]:
        # A bookmark whose frozen raw block can no longer be proven must fail
        # loudly instead of navigating the reviewer to a wrong waveform.
        serialized = BookmarkResolutionOut.model_validate(resolved).model_dump(mode="json")
        detail = {
            "code": resolved["error_code"],
            "message": resolved["error_message"],
            "resolution": serialized,
        }
        raise HTTPException(409, detail)
    return resolved
