from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app.photo_sources import google_photos

router = APIRouter(prefix="/auth/google", tags=["auth"])


@router.get("/login")
def login():
    return RedirectResponse(google_photos.build_auth_url())


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"Google auth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    google_photos.exchange_code(code, state)
    return {"status": "connected", "detail": "Google Photos account linked. You can close this tab."}
