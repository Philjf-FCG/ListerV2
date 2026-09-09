from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.db import init_db
from app.routers import auth, ebay, ebay_auth, listings, photos, sync

_APP_DIR = Path(__file__).parent

app = FastAPI(title="Lister", description="Photo -> humorous listing -> Vinted/eBay draft pipeline")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.extension_origin] if settings.extension_origin != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(photos.router)
app.include_router(listings.router)
app.include_router(auth.router)
app.include_router(ebay_auth.router)
app.include_router(ebay.router)
app.include_router(sync.router)

app.mount("/static", StaticFiles(directory=str(_APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    try:
        sync.relink_all_vinted_photos()
    except Exception:
        pass


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}
