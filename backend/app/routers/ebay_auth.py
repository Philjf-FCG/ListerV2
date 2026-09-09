from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from app import ebay_client

router = APIRouter(prefix="/auth/ebay", tags=["auth"])


@router.get("/login")
def login():
    return RedirectResponse(ebay_client.build_auth_url())


@router.get("/callback")
def callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"eBay auth error: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")
    try:
        ebay_client.exchange_code(code, state)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"status": "connected", "detail": "eBay account linked. You can close this tab."}
