"""Tests for the Vinted -> eBay sync photo caching fix.

The bug: Vinted's scraped CDN photo URLs are signed with a short-lived token, so by
the time a listing is imported (often much later) the URLs have already expired -
photos silently disappear and eBay drafts fall back to a placeholder image. The fix
downloads photos immediately when the extension reports them (see
app.routers.sync.cache_vinted_photos), so import always has real local files to
work from regardless of URL freshness.
"""
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
import io

from app.routers import sync


def _fake_jpeg_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color="red").save(buf, "JPEG")
    return buf.getvalue()


def test_vinted_item_id_extracts_numeric_id():
    assert sync._vinted_item_id("https://www.vinted.co.uk/items/10030090411-a-title") == "10030090411"
    assert sync._vinted_item_id("https://www.vinted.co.uk/items/42") == "42"


def test_vinted_item_id_returns_none_for_non_matching_url():
    assert sync._vinted_item_id("https://www.vinted.co.uk/catalog") is None
    assert sync._vinted_item_id(None) is None


def test_cache_vinted_photos_downloads_and_saves_each_url(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_PHOTO_CACHE_DIR", tmp_path)

    fake_resp = MagicMock()
    fake_resp.content = _fake_jpeg_bytes()
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.get.return_value = fake_resp
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    with patch.object(sync.httpx, "Client", return_value=fake_client):
        cached = sync.cache_vinted_photos("12345", ["https://images1.vinted.net/a.jpg", "https://images1.vinted.net/b.jpg"])

    assert len(cached) == 2
    assert all(p.exists() for p in cached)
    assert fake_client.get.call_count == 2


def test_cache_vinted_photos_skips_failed_downloads_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_PHOTO_CACHE_DIR", tmp_path)

    ok_resp = MagicMock()
    ok_resp.content = _fake_jpeg_bytes()
    ok_resp.raise_for_status = MagicMock()

    fake_client = MagicMock()
    fake_client.get.side_effect = [Exception("404 expired"), ok_resp]
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    with patch.object(sync.httpx, "Client", return_value=fake_client):
        cached = sync.cache_vinted_photos("999", ["https://images1.vinted.net/expired.jpg", "https://images1.vinted.net/ok.jpg"])

    # Only the second URL succeeded; the first was skipped, not raised.
    assert len(cached) == 1


def test_get_local_vinted_photos_prefers_cache_over_configured_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "_PHOTO_CACHE_DIR", tmp_path)
    item_folder = tmp_path / "555"
    item_folder.mkdir()
    (item_folder / "photo_0.jpg").write_bytes(_fake_jpeg_bytes())

    photos = sync.get_local_vinted_photos("https://www.vinted.co.uk/items/555-thing")
    assert len(photos) == 1
    assert photos[0].name == "photo_0.jpg"


def test_get_local_vinted_photos_returns_empty_for_url_without_item_id():
    assert sync.get_local_vinted_photos("https://www.vinted.co.uk/catalog") == []
    assert sync.get_local_vinted_photos(None) == []
