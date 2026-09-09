import os
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from app.photo_sources import google_photos, local
from app.schemas import PhotoFilter

router = APIRouter(prefix="/photos", tags=["photos"])


@router.get("/local")
def list_local_photos(
    date: str | None = None,
    taken_today: bool = False,
    after_time: str | None = None,
    before_time: str | None = None,
    limit: int = 25,
):
    filt = PhotoFilter(
        date=date, taken_today=taken_today, after_time=after_time, before_time=before_time, limit=limit
    )
    return local.scan_local_photos(filt)


@router.get("/thumbnail")
def get_thumbnail(path: str = Query(...)):
    # Extract just the filename from the path parameter (handles full paths, query strings, etc.)
    if "/" in path or "\\" in path:
        if "?" in path:
            path = path.split("?")[-1]
            if "=" in path:
                path = path.split("=")[-1]
    path = os.path.basename(path)
    
    file_path = local.resolve_thumbnail_file(path)
    if file_path is None:
        return JSONResponse(status_code=404, content={"detail": "thumbnail not found"})
    return FileResponse(file_path, media_type="image/jpeg")


@router.get("/google/status")
def google_status():
    return {"connected": google_photos.is_connected()}


@router.post("/google/session")
def create_google_session():
    try:
        return google_photos.create_picker_session()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/google/session/{session_id}")
def get_google_session(session_id: str):
    try:
        return google_photos.get_picker_session(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/google/items/{session_id}")
def get_google_items(session_id: str):
    try:
        return google_photos.list_picked_items(session_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/google/thumbnail/{media_item_id}")
def get_google_thumbnail(media_item_id: str):
    try:
        content, content_type = google_photos.fetch_thumbnail(media_item_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=content, media_type=content_type)








