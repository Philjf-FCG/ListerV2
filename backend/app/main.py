import base64
import secrets
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.db import init_db
from app.routers import auth, ebay, ebay_auth, listings, photos, refresh, rotate, sync

_APP_DIR = Path(__file__).parent

app = FastAPI(title="Lister", description="Photo -> humorous listing -> Vinted/eBay draft pipeline")

settings = get_settings()


@app.middleware("http")
async def require_access_password(request: Request, call_next):
    """HTTP Basic gate for network-reachable deployments (see Settings.access_password).
    Registered before CORSMiddleware so CORS is the outer layer: a 401 then still carries
    CORS headers and the browser extension can read it instead of seeing an opaque failure."""
    password = get_settings().access_password
    if not password or request.method == "OPTIONS" or request.url.path == "/healthz":
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            user = supplied = ""
        if secrets.compare_digest(supplied.encode(), password.encode()) and secrets.compare_digest(
            user.encode(), get_settings().access_user.encode()
        ):
            return await call_next(request)
    return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="Lister"'})


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
app.include_router(refresh.router)
app.include_router(rotate.router)

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
