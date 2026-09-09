"""Local filesystem photo ingestion with date/time filtering based on EXIF data."""
import hashlib
from datetime import datetime, time
from pathlib import Path

from PIL import Image, ExifTags

from app.config import BACKEND_DIR, get_settings
from app.schemas import PhotoFilter, PhotoItem

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}
_DATETIME_TAG = next((k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"), 36867)

_THUMB_DIR = BACKEND_DIR / "thumbnails"


def _thumbnail_dir() -> Path:
    _THUMB_DIR.mkdir(parents=True, exist_ok=True)
    return _THUMB_DIR


def _taken_at(path: Path) -> datetime | None:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get(_DATETIME_TAG)
            if raw:
                return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def ensure_thumbnail(path: Path) -> Path:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    thumb_path = _thumbnail_dir() / f"{digest}.jpg"
    if not thumb_path.exists():
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((512, 512))
            img.save(thumb_path, "JPEG", quality=85)
    return thumb_path


def _matches_filter(taken_at: datetime | None, filt: PhotoFilter) -> bool:
    if filt.taken_today and taken_at is not None:
        if taken_at.date() != datetime.now().date():
            return False
    if filt.date and taken_at is not None:
        target = datetime.strptime(filt.date, "%Y-%m-%d").date()
        if taken_at.date() != target:
            return False
    if taken_at is not None:
        t = taken_at.time()
        if filt.after_time:
            after = time.fromisoformat(filt.after_time)
            if t < after:
                return False
        if filt.before_time:
            before = time.fromisoformat(filt.before_time)
            if t > before:
                return False
    return True


def scan_local_photos(filt: PhotoFilter) -> list[PhotoItem]:
    settings = get_settings()
    results: list[PhotoItem] = []
    for root in settings.local_photo_root_list:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for file_path in root_path.rglob("*"):
            if file_path.suffix.lower() not in _IMAGE_EXTS or not file_path.is_file():
                continue
            taken_at = _taken_at(file_path)
            if not _matches_filter(taken_at, filt):
                continue
            thumb = ensure_thumbnail(file_path)
            results.append(
                PhotoItem(
                    source="local",
                    ref=str(file_path),
                    thumbnail_url=f"/photos/thumbnail?path={thumb.name}",
                    taken_at=taken_at,
                )
            )
            if len(results) >= filt.limit:
                return results
    return results


def resolve_thumbnail_file(name: str) -> Path | None:
    candidate = _thumbnail_dir() / name
    return candidate if candidate.exists() else None
