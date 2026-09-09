"""eBay Sell API client (Production environment).

Creates real DRAFT listings via the official Inventory API - an "offer" that is
never published (we never call POST /offer/{id}/publish) shows up for the seller
under Seller Hub > Drafts, ready for them to review and list manually. This avoids
all the DOM-scraping fragility of browser automation.

One-time prerequisites on the seller's eBay account (can't be automated by us):
  - Business policies (fulfillment/payment/return) must exist - created once via
    Seller Hub > Account > Business policies, or left as eBay's auto-created
    defaults if the seller has listed before.
  - A merchant inventory location - this module will auto-create a basic one from
    EBAY_LOCATION_* config if none exists.

Flow:
  1. GET /auth/ebay/login    -> redirects to eBay's consent page
  2. GET /auth/ebay/callback -> exchanges code for a user token, stores it locally
  3. GET /listings/{id}/ebay/category-suggestions -> Taxonomy API suggestions
  4. POST /listings/{id}/push-to-ebay -> creates inventory item + unpublished offer
"""
import base64
import io
import json
import re
import time as time_mod
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from PIL import Image

from app.config import BACKEND_DIR, get_settings

_AUTH_BASE = "https://auth.ebay.com/oauth2/authorize"
_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_API_BASE = "https://api.ebay.com"
_TRADING_API_URL = "https://api.ebay.com/ws/api.dll"

_USER_SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
]
_APP_SCOPES = ["https://api.ebay.com/oauth/api_scope"]

_TOKEN_FILE = BACKEND_DIR / "ebay_token.json"
_PENDING_STATES: set[str] = set()

_app_token_cache: dict = {}
_eps_cache: dict[str, str] = {}


def _basic_auth_header() -> dict:
    settings = get_settings()
    creds = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode("ascii")
    return {"Authorization": f"Basic {base64.b64encode(creds).decode('ascii')}"}


def _raise_with_ebay_detail(resp: httpx.Response) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"eBay API error {resp.status_code}: {resp.text}") from exc


# --- User OAuth (authorization code grant) ---------------------------------


def build_auth_url() -> str:
    from urllib.parse import quote, urlencode

    settings = get_settings()
    state = base64.urlsafe_b64encode(str(time_mod.time()).encode()).decode().rstrip("=")
    _PENDING_STATES.add(state)
    params = {
        "client_id": settings.ebay_client_id,
        "redirect_uri": settings.ebay_ru_name,
        "response_type": "code",
        "scope": " ".join(_USER_SCOPES),
        "state": state,
    }
    return f"https://auth2.ebay.com/oauth2/consents?{urlencode(params, quote_via=quote)}"


def exchange_code(code: str, state: str | None = None) -> None:
    settings = get_settings()
    if state:
        _PENDING_STATES.discard(state)
    with httpx.Client() as client:
        resp = client.post(
            _TOKEN_URL,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.ebay_ru_name,
            },
        )
        _raise_with_ebay_detail(resp)
        data = resp.json()
    data["obtained_at"] = time_mod.time()
    _TOKEN_FILE.write_text(json.dumps(data))


def is_connected() -> bool:
    return _TOKEN_FILE.exists()


def _refresh_user_token(data: dict) -> dict:
    with httpx.Client() as client:
        resp = client.post(
            _TOKEN_URL,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
                "scope": "%20".join(_USER_SCOPES),
            },
        )
        _raise_with_ebay_detail(resp)
        refreshed = resp.json()
    # eBay doesn't resend the refresh_token on refresh - keep the original.
    refreshed["refresh_token"] = data["refresh_token"]
    refreshed["obtained_at"] = time_mod.time()
    _TOKEN_FILE.write_text(json.dumps(refreshed))
    return refreshed


def _user_access_token() -> str:
    if not _TOKEN_FILE.exists():
        raise RuntimeError("eBay is not connected yet; visit /auth/ebay/login first")
    data = json.loads(_TOKEN_FILE.read_text())
    if time_mod.time() > data["obtained_at"] + data["expires_in"] - 60:
        data = _refresh_user_token(data)
    return data["access_token"]


def _user_auth_header() -> dict:
    return {"Authorization": f"Bearer {_user_access_token()}"}


# --- Application token (client credentials grant), used for Taxonomy lookups ---


def _app_access_token() -> str:
    cached = _app_token_cache
    if cached and time_mod.time() < cached.get("expires_at", 0):
        return cached["access_token"]
    with httpx.Client() as client:
        resp = client.post(
            _TOKEN_URL,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "%20".join(_APP_SCOPES)},
        )
        _raise_with_ebay_detail(resp)
        data = resp.json()
    _app_token_cache["access_token"] = data["access_token"]
    _app_token_cache["expires_at"] = time_mod.time() + data["expires_in"] - 60
    return data["access_token"]


def _app_auth_header() -> dict:
    return {"Authorization": f"Bearer {_app_access_token()}"}


# --- Taxonomy API: category suggestions -------------------------------------


def get_category_tree_id() -> str:
    settings = get_settings()
    with httpx.Client() as client:
        resp = client.get(
            f"{_API_BASE}/commerce/taxonomy/v1/get_default_category_tree_id",
            headers=_app_auth_header(),
            params={"marketplace_id": settings.ebay_marketplace_id},
        )
        _raise_with_ebay_detail(resp)
        return resp.json()["categoryTreeId"]


def get_category_suggestions(query: str) -> list[dict]:
    """Returns up to a handful of {category_id, category_name, full_path} suggestions."""
    tree_id = get_category_tree_id()
    with httpx.Client() as client:
        resp = client.get(
            f"{_API_BASE}/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions",
            headers=_app_auth_header(),
            params={"q": query},
        )
        _raise_with_ebay_detail(resp)
        data = resp.json()
    suggestions = []
    for item in data.get("categorySuggestions", []):
        category = item.get("category", {})
        ancestors = item.get("categoryTreeNodeAncestors", [])
        path = " > ".join([a.get("categoryName", "") for a in reversed(ancestors)] + [category.get("categoryName", "")])
        
        # Filter out categories that don't match the query context
        # (e.g., LEGO suggestions for non-LEGO items, Jigsaw for non-jigsaw items)
        category_name_lower = (category.get("categoryName") or "").lower()
        query_lower = query.lower()
        
        # Skip if the category is highly specific to a brand/keyword that doesn't match the query
        exclusive_keywords = ["lego", "jigsaw", "nike", "adidas", "lenovo"]  # Add more as needed
        for kw in exclusive_keywords:
            if kw in category_name_lower and kw not in query_lower:
                continue  # Skip this suggestion
        
        suggestions.append({
            "category_id": category.get("categoryId"),
            "category_name": category.get("categoryName"),
            "full_path": path,
        })
    return suggestions


def get_category_required_aspects(category_id: str) -> list[str]:
    """Returns the names of item aspects marked required by eBay for this category."""
    tree_id = get_category_tree_id()
    with httpx.Client() as client:
        resp = client.get(
            f"{_API_BASE}/commerce/taxonomy/v1/category_tree/{tree_id}/get_item_aspects_for_category",
            headers=_app_auth_header(),
            params={"category_id": category_id},
        )
        _raise_with_ebay_detail(resp)
        aspects = resp.json().get("aspects", [])
    return [
        a["localizedAspectName"]
        for a in aspects
        if a.get("aspectConstraint", {}).get("aspectRequired")
    ]


# --- Account API: merchant location + business policies ---------------------


def get_or_create_merchant_location() -> str:
    settings = get_settings()
    with httpx.Client() as client:
        resp = client.get(f"{_API_BASE}/sell/inventory/v1/location", headers=_user_auth_header())
        _raise_with_ebay_detail(resp)
        locations = resp.json().get("locations", [])
        if locations:
            return locations[0]["merchantLocationKey"]

        if not settings.ebay_location_postal_code:
            raise RuntimeError(
                "No merchant inventory location exists on your eBay account, and "
                "EBAY_LOCATION_POSTAL_CODE isn't set in .env to auto-create one. "
                "Set it (and EBAY_LOCATION_COUNTRY) then try again."
            )
        key = "lister-default-location"
        create_resp = client.post(
            f"{_API_BASE}/sell/inventory/v1/location/{key}",
            headers={**_user_auth_header(), "Content-Type": "application/json"},
            json={
                "location": {
                    "address": {
                        "postalCode": settings.ebay_location_postal_code,
                        "country": settings.ebay_location_country,
                    }
                },
                "locationTypes": ["WAREHOUSE"],
                "name": "Lister default location",
            },
        )
        _raise_with_ebay_detail(create_resp)
        return key


def get_business_policy_ids() -> dict:
    """Returns the first fulfillment/payment/return policy IDs found on the account.
    eBay requires these to exist already (set up once via Seller Hub > Business policies)."""
    settings = get_settings()
    policy_kinds = [
        ("fulfillmentPolicyId", "fulfillment_policy", "fulfillmentPolicies"),
        ("paymentPolicyId", "payment_policy", "paymentPolicies"),
        ("returnPolicyId", "return_policy", "returnPolicies"),
    ]
    result = {}
    with httpx.Client() as client:
        for field, endpoint, json_key in policy_kinds:
            resp = client.get(
                f"{_API_BASE}/sell/account/v1/{endpoint}",
                headers=_user_auth_header(),
                params={"marketplace_id": settings.ebay_marketplace_id},
            )
            _raise_with_ebay_detail(resp)
            policies = resp.json().get(json_key, [])
            if not policies:
                raise RuntimeError(
                    f"No {endpoint.replace('_', ' ')} found on your eBay account. Set one up once via "
                    "Seller Hub > Account > Business policies, then try again."
                )
            result[field] = policies[0][field]
    return result


# --- Inventory API: create the draft ----------------------------------------


def _format_html_description(text: str) -> str:
    if not text:
        return ""
    if "<p>" in text.lower() or "<br" in text.lower() or "<div>" in text.lower():
        return text
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return ""
    return "".join(f"<p>{p}</p>" for p in paragraphs)


def _clean_price(price_str: str | None) -> str:
    if not price_str:
        return "1.00"
    cleaned = re.sub(r"[^\d.]", "", price_str)
    try:
        val = float(cleaned)
        return f"{val:.2f}"
    except (ValueError, TypeError):
        return "1.00"


def get_category_allowed_condition_ids(category_id: str) -> list[str]:
    """Retrieves allowed condition IDs (e.g. 1000, 3000, 5000) for an eBay category."""
    settings = get_settings()
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                f"{_API_BASE}/sell/metadata/v1/marketplace/{settings.ebay_marketplace_id}/get_item_condition_policies",
                headers=_user_auth_header(),
                params={"filter": f"categoryIds:{{{category_id}}}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                for p in data.get("itemConditionPolicies", []):
                    if str(p.get("categoryId")) == str(category_id):
                        return [str(c.get("conditionId")) for c in p.get("itemConditions", [])]
    except Exception:
        pass
    return []


def map_condition_for_category(category_id: str | None, condition_str: str | None) -> str:
    """Maps a user condition string to an Inventory API ConditionEnum or condition ID that is valid
    for the specific category (e.g. Books requires '4000' for Very Good, not 'USED_EXCELLENT')."""
    cond = (condition_str or "").strip().lower()
    allowed_ids = get_category_allowed_condition_ids(category_id) if category_id else []

    # If category has specific condition IDs (like Books: 1000=New, 2750=Like New, 4000=Very Good, etc.)
    if allowed_ids:
        # Check for 'new without tag' / 'new other' / 'new with defect' first
        if "without tag" in cond or "no tag" in cond or "new without" in cond or "new other" in cond:
            if "2990" in allowed_ids:
                return "2990"
            if "NEW_OTHER" in allowed_ids:
                return "NEW_OTHER"
        if "defect" in cond or "imperfection" in cond:
            if "2980" in allowed_ids:
                return "2980"
            if "NEW_WITH_DEFECTS" in allowed_ids:
                return "NEW_WITH_DEFECTS"

        # Check for specific new conditions first
        if "like new" in cond or "new without tag" in cond:
            if "2750" in allowed_ids:
                return "2750"
            if "1000" in allowed_ids:
                return "1000"
        elif "new" in cond and "tag" not in cond:
            if "1000" in allowed_ids:
                return "1000"

        # For used items, try to match the closest condition ID
        if "very good" in cond or "excellent" in cond:
            if "4000" in allowed_ids:
                return "4000"
            if "USED_EXCELLENT" in allowed_ids:
                return "USED_EXCELLENT"
        if "good" in cond and "very" not in cond:
            if "5000" in allowed_ids:
                return "5000"
            if "USED_GOOD" in allowed_ids:
                return "USED_GOOD"
        if "fair" in cond or "acceptable" in cond:
            if "6000" in allowed_ids:
                return "6000"
            if "USED_ACCEPTABLE" in allowed_ids:
                return "USED_ACCEPTABLE"

    # Standard enum mapping for categories that don't require specific IDs
    if not allowed_ids or "5000" in allowed_ids:
        if "fair" in cond or "acceptable" in cond:
            return "USED_ACCEPTABLE"
        if "good" in cond and "very" not in cond:
            return "USED_GOOD"
        if "very good" in cond or "excellent" in cond or "like new" in cond:
            return "USED_EXCELLENT"
        return "USED_GOOD"

    # If category only allows 3000 / 2990 (e.g. Clothing/Shoes which only accept Pre-owned)
    if "3000" in allowed_ids or "2990" in allowed_ids:
        return "USED_EXCELLENT"

    return "USED_EXCELLENT"



def upload_picture_to_ebay(photo_path: str | Path) -> str:
    """Uploads a local image file directly to eBay Picture Services (EPS) using the
    Trading API UploadSiteHostedPictures call, returning the hosted FullURL."""
    path = Path(photo_path)
    if not path.exists():
        raise FileNotFoundError(f"Photo file not found: {path}")

    cache_key = f"{path.resolve()}_{path.stat().st_mtime}"
    if cache_key in _eps_cache:
        return _eps_cache[cache_key]

    with Image.open(path) as img:
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        jpeg_bytes = buf.getvalue()

    xml_part = """<?xml version="1.0" encoding="utf-8"?>
<UploadSiteHostedPicturesRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <PictureSet>Standard</PictureSet>
  <PictureUploadPolicy>Add</PictureUploadPolicy>
</UploadSiteHostedPicturesRequest>"""

    files = {
        "XML Payload": (None, xml_part, "text/xml;charset=utf-8"),
        "photo.jpg": ("photo.jpg", jpeg_bytes, "image/jpeg"),
    }

    with httpx.Client(timeout=45.0) as client:
        resp = client.post(
            _TRADING_API_URL,
            headers={
                "X-EBAY-API-SITEID": "3",  # eBay UK
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1349",
                "X-EBAY-API-CALL-NAME": "UploadSiteHostedPictures",
                "X-EBAY-API-IAF-TOKEN": _user_access_token(),
            },
            files=files,
        )
        resp.raise_for_status()

    root = ET.fromstring(resp.text)
    ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
    ack = root.findtext(".//e:Ack", namespaces=ns) or root.findtext(".//Ack")
    if ack not in ("Success", "Warning"):
        err_msg = root.findtext(".//e:LongMessage", namespaces=ns) or root.findtext(".//LongMessage") or resp.text
        raise RuntimeError(f"eBay EPS upload failed: {err_msg}")

    full_url = root.findtext(".//e:FullURL", namespaces=ns) or root.findtext(".//FullURL")
    if not full_url:
        raise RuntimeError(f"eBay EPS did not return FullURL: {resp.text}")

    _eps_cache[cache_key] = full_url
    return full_url


def create_or_replace_inventory_item(
    sku: str,
    title: str,
    description: str,
    condition: str,
    category_id: str | None = None,
    image_urls: list[str] | None = None,
    aspects: dict[str, list[str]] | None = None,
) -> None:
    product_payload = {
        "title": title[:80],
        "description": _format_html_description(description),
    }
    if image_urls:
        product_payload["imageUrls"] = image_urls[:12]
    if aspects:
        product_payload["aspects"] = aspects

    resolved_condition = map_condition_for_category(category_id, condition)

    with httpx.Client(timeout=120.0) as client:
        resp = client.put(
            f"{_API_BASE}/sell/inventory/v1/inventory_item/{sku}",
            headers={
                **_user_auth_header(),
                "Content-Type": "application/json",
                "Content-Language": "en-GB",
            },
            json={
                "product": product_payload,
                "condition": resolved_condition,
                "availability": {"shipToLocationAvailability": {"quantity": 1}},
            },
        )
        _raise_with_ebay_detail(resp)


def create_offer(sku: str, category_id: str, price: str) -> dict:
    settings = get_settings()
    location_key = get_or_create_merchant_location()
    policies = get_business_policy_ids()
    clean_price_val = _clean_price(price)
    headers = {
        **_user_auth_header(),
        "Content-Type": "application/json",
        "Content-Language": "en-GB",
    }
    offer_payload = {
        "sku": sku,
        "marketplaceId": settings.ebay_marketplace_id,
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": category_id,
        "listingPolicies": policies,
        "merchantLocationKey": location_key,
        "pricingSummary": {
            "price": {"value": clean_price_val, "currency": "GBP"},
        },
    }

    with httpx.Client() as client:
        # Check if an offer already exists for this SKU
        existing_resp = client.get(
            f"{_API_BASE}/sell/inventory/v1/offer?sku={sku}",
            headers=_user_auth_header(),
        )
        if existing_resp.status_code == 200:
            existing_offers = existing_resp.json().get("offers", [])
            if existing_offers:
                offer_id = existing_offers[0]["offerId"]
                put_resp = client.put(
                    f"{_API_BASE}/sell/inventory/v1/offer/{offer_id}",
                    headers=headers,
                    json=offer_payload,
                )
                _raise_with_ebay_detail(put_resp)
                return {"offerId": offer_id}

        resp = client.post(
            f"{_API_BASE}/sell/inventory/v1/offer",
            headers=headers,
            json=offer_payload,
        )
        _raise_with_ebay_detail(resp)
        return resp.json()  # includes offerId - NOT published, i.e. it's a draft


def publish_offer(offer_id: str) -> dict:
    """Publishes an inventory offer to live eBay and returns {'listingId': '...'}'."""
    with httpx.Client() as client:
        resp = client.post(
            f"{_API_BASE}/sell/inventory/v1/offer/{offer_id}/publish",
            headers={
                **_user_auth_header(),
                "Content-Type": "application/json",
            },
        )
        _raise_with_ebay_detail(resp)
        return resp.json()


# --- Trading API: existing live listings (for the Vinted<->eBay sync feature) ---
# The Inventory API above only sees items created through the Inventory/Sell API
# itself - a seller's pre-existing listings (created via eBay's own site) won't
# show up there. GetMyeBaySelling on the older Trading API sees everything, and
# accepts the same OAuth user token via the X-EBAY-API-IAF-TOKEN header.

_TRADING_API_URL = "https://api.ebay.com/ws/api.dll"
_GET_MY_EBAY_SELLING_XML = """<?xml version="1.0" encoding="utf-8"?>
<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <ActiveList>
    <Include>true</Include>
    <Pagination>
      <EntriesPerPage>200</EntriesPerPage>
      <PageNumber>1</PageNumber>
    </Pagination>
  </ActiveList>
</GetMyeBaySellingRequest>"""


def get_active_listing_titles() -> list[str]:
    import xml.etree.ElementTree as ET

    with httpx.Client() as client:
        resp = client.post(
            _TRADING_API_URL,
            headers={
                "X-EBAY-API-SITEID": "3",  # eBay UK
                "X-EBAY-API-COMPATIBILITY-LEVEL": "1349",
                "X-EBAY-API-CALL-NAME": "GetMyeBaySelling",
                "X-EBAY-API-IAF-TOKEN": _user_access_token(),
                "Content-Type": "text/xml",
            },
            content=_GET_MY_EBAY_SELLING_XML,
        )
        resp.raise_for_status()
    ns = {"e": "urn:ebay:apis:eBLBaseComponents"}
    root = ET.fromstring(resp.text)
    ack = root.findtext("e:Ack", namespaces=ns)
    if ack not in ("Success", "Warning"):
        errors = [e.findtext("e:LongMessage", namespaces=ns) for e in root.findall("e:Errors", ns)]
        raise RuntimeError(f"eBay GetMyeBaySelling failed: {'; '.join(filter(None, errors)) or ack}")
    titles = [
        title_el.text
        for title_el in root.findall(".//e:ActiveList/e:ItemArray/e:Item/e:Title", ns)
        if title_el.text
    ]
    return titles
