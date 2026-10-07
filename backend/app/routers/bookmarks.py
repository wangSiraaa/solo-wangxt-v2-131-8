from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..bookmarks import BookmarkError, create_bookmark, ensure_source_valid, locate_bookmark
from ..database import get_db
from ..models import Report, WaveformBookmark
from ..schemas import BookmarkCreate, BookmarkLocateOut, BookmarkOut

router = APIRouter(tags=["bookmarks"])


def _raise_bookmark_error(exc: BookmarkError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, **exc.details},
    ) from exc


def _with_source_status(db: Session, bookmark: WaveformBookmark) -> dict:
    data = BookmarkOut.model_validate(bookmark).model_dump()
    try:
        ensure_source_valid(db, bookmark)
    except BookmarkError as exc:
        data["source_valid"] = False
        data["source_error"] = {"code": exc.code, "message": exc.message, **exc.details}
    else:
        data["source_valid"] = True
        data["source_error"] = None
    return data


@router.post("/reports/{report_id}/bookmarks", response_model=BookmarkOut, status_code=status.HTTP_201_CREATED)
def post_bookmark(report_id: str, payload: BookmarkCreate, db: Session = Depends(get_db)):
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(404, detail={"code": "report_not_found", "message": "report not found"})
    try:
        bookmark = create_bookmark(db, report=report, **payload.model_dump())
    except BookmarkError as exc:
        _raise_bookmark_error(exc)
    db.commit()
    db.refresh(bookmark)
    return _with_source_status(db, bookmark)


@router.get("/reports/{report_id}/bookmarks", response_model=list[BookmarkOut])
def list_bookmarks(report_id: str, db: Session = Depends(get_db)):
    if db.get(Report, report_id) is None:
        raise HTTPException(404, detail={"code": "report_not_found", "message": "report not found"})
    bookmarks = db.scalars(
        select(WaveformBookmark)
        .where(WaveformBookmark.report_id == report_id)
        .order_by(WaveformBookmark.display_seconds, WaveformBookmark.created_at)
    ).all()
    return [_with_source_status(db, bookmark) for bookmark in bookmarks]


@router.get("/waveform-bookmarks/{bookmark_id}/locate", response_model=BookmarkLocateOut)
def locate(bookmark_id: str, db: Session = Depends(get_db)):
    bookmark = db.get(WaveformBookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(404, detail={"code": "bookmark_not_found", "message": "bookmark not found"})
    try:
        return locate_bookmark(db, bookmark)
    except BookmarkError as exc:
        _raise_bookmark_error(exc)


@router.delete("/waveform-bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bookmark(bookmark_id: str, db: Session = Depends(get_db)):
    # Only the bookmark row is removed. The report, its metrics/quality state,
    # chunk metadata and raw objects are deliberately untouched.
    bookmark = db.get(WaveformBookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(404, detail={"code": "bookmark_not_found", "message": "bookmark not found"})
    db.delete(bookmark)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
