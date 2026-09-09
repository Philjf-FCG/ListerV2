from datetime import datetime
from typing import Literal

from pydantic import BaseModel


Platform = Literal["vinted", "ebay"]
Source = Literal["local", "google_photos", "url"]
ListingStatus = Literal["draft_pending", "reviewed_ready", "posted_as_draft", "failed"]


class PhotoFilter(BaseModel):
    """Parameters used to select which photos to ingest."""
    date: str | None = None            # e.g. "2026-08-31"; defaults to today when taken_today=True
    taken_today: bool = False
    after_time: str | None = None      # "HH:MM", e.g. "12:00"
    before_time: str | None = None     # "HH:MM"
    limit: int = 25


class PhotoItem(BaseModel):
    source: Source
    ref: str                # file path or Google media item id
    thumbnail_url: str
    taken_at: datetime | None = None


class PhotoRef(BaseModel):
    """A single photo reference within a (possibly multi-photo) listing."""
    source: Source
    ref: str


class GenerateRequest(BaseModel):
    items: list[PhotoRef]          # one or more photos of the same item
    platforms: list[Platform] = ["vinted", "ebay"]
    item_hint: str | None = None   # optional free-text hint, e.g. "men's North Face jacket, size L"
    condition: str | None = None
    price_hint: str | None = None


class ListingOut(BaseModel):
    id: int
    photo_items: list[PhotoRef]
    thumbnail_urls: list[str]
    platform: Platform
    title: str | None
    description: str | None
    price: str | None
    condition: str | None
    tags: str | None
    status: ListingStatus
    error: str | None
    created_at: str
    updated_at: str


class ListingUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    price: str | None = None
    condition: str | None = None
    tags: str | None = None
    status: ListingStatus | None = None


class VintedItemIn(BaseModel):
    """A single item scraped from the user's own Vinted listings page by the extension."""
    url: str
    title: str
    price: str | None = None
    photo_urls: list[str] | None = None  # Changed from photo_url to accept multiple photos


class VintedItemOut(BaseModel):
    id: int
    url: str
    title: str
    price: str | None
    photo_urls: list[str] | None  # Changed from photo_url to match input schema
    imported_listing_id: int | None
    scraped_at: str
    on_ebay: bool = False
