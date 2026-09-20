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
import uuid
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


def generate_ebay_sku() -> str:
    """A new SKU that's safe to use forever - unlike the old f"lister-{row_id}"
    scheme, this is never derived from the local SQLite row id, so it can never
    collide with a real listing from a previous life of the local database (row
    ids get reused after any DB reset, which happened more than once during this
    project's history and silently aimed pushes at unrelated live eBay listings).
    Callers must persist this on the listing row and reuse it on every retry -
    never call this more than once per listing."""
    return f"lister-{uuid.uuid4().hex[:12]}"


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
                # httpx form-encodes dict values itself - a pre-escaped "%20" here gets
                # double-encoded into "%2520", which eBay rejects as invalid_scope. Use a
                # real space (like build_auth_url does) and let httpx encode it once.
                "scope": " ".join(_USER_SCOPES),
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
            data={"grant_type": "client_credentials", "scope": " ".join(_APP_SCOPES)},
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


def get_category_required_aspects(category_id: str) -> list[dict]:
    """Returns the item aspects marked required by eBay for this category, each as
    {"name": ..., "mode": "FREE_TEXT" | "SELECTION_ONLY", "values": [...]}.

    `values` lists eBay's standard values for SELECTION_ONLY aspects (e.g. "UK Shoe
    Size") - since some categories no longer accept arbitrary free text for these,
    a caller must pick one of these values rather than inventing its own."""
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
        {
            "name": a["localizedAspectName"],
            "mode": a.get("aspectConstraint", {}).get("aspectMode", "FREE_TEXT"),
            "values": [
                v["localizedValue"]
                for v in a.get("aspectValues", [])
                if v.get("localizedValue")
            ],
        }
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
    """Retrieves the numeric condition IDs (e.g. 1000, 3000, 5000) eBay's Metadata
    API allows for a category - NOT valid values for the Inventory API's "condition"
    field itself (see map_condition_for_category), only used to figure out which
    ConditionEnum names that category will accept at publish time."""
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


# eBay's numeric conditionId -> Sell Inventory API ConditionEnum name. These IDs are
# constant across the whole site (a category's own conditionDescription text just
# rewords the same ID for context - e.g. Trainers labels 3000 "Pre-owned - Good"
# while the underlying enum is USED_EXCELLENT); what varies per category is only
# which of these IDs get_category_allowed_condition_ids() says are allowed.
_CONDITION_ID_TO_ENUM = {
    "1000": "NEW",
    "1500": "NEW_OTHER",
    "1750": "NEW_WITH_DEFECTS",
    "2000": "MANUFACTURER_REFURBISHED",
    "2010": "CERTIFIED_REFURBISHED",
    "2020": "EXCELLENT_REFURBISHED",
    "2030": "VERY_GOOD_REFURBISHED",
    "2040": "GOOD_REFURBISHED",
    "2500": "SELLER_REFURBISHED",
    "2750": "LIKE_NEW",
    "2990": "PRE_OWNED_EXCELLENT",
    "3000": "USED_EXCELLENT",
    "3010": "PRE_OWNED_FAIR",
    "4000": "USED_VERY_GOOD",
    "5000": "USED_GOOD",
    "6000": "USED_ACCEPTABLE",
    "7000": "FOR_PARTS_OR_NOT_WORKING",
}


def map_condition_for_category(category_id: str | None, condition_str: str | None) -> str:
    """Maps a free-text condition string to a Sell Inventory API ConditionEnum value
    that this category will actually accept.

    Fashion categories like Trainers only allow a *subset* of the standard enums
    (their own get_item_condition_policies list is {NEW, NEW_OTHER, NEW_WITH_DEFECTS,
    PRE_OWNED_EXCELLENT, USED_EXCELLENT, PRE_OWNED_FAIR} - notably no LIKE_NEW,
    USED_VERY_GOOD, USED_GOOD or USED_ACCEPTABLE). Sending an enum outside that set
    passes inventory-item PUT (which only checks the string is a real enum name) but
    fails at offer publish with errorId 25021 ("condition id is invalid for the
    selected primary category id") - confirmed live for USED_GOOD against Trainers.
    So this picks the best-matching enum from an ordered candidate list per condition,
    falling back down the list to whichever candidate the category actually allows.
    """
    cond = (condition_str or "").strip().lower()

    if "defect" in cond or "imperfection" in cond:
        candidates = ["NEW_WITH_DEFECTS"]
    elif "without tag" in cond or "no tag" in cond or "new other" in cond or "new without" in cond:
        candidates = ["NEW_OTHER"]
    elif "like new" in cond:
        candidates = ["LIKE_NEW", "PRE_OWNED_EXCELLENT", "USED_EXCELLENT"]
    elif "new" in cond:
        candidates = ["NEW"]
    elif "very good" in cond or "excellent" in cond:
        candidates = ["USED_VERY_GOOD", "PRE_OWNED_EXCELLENT", "USED_EXCELLENT"]
    elif "good" in cond:
        candidates = ["USED_GOOD", "USED_EXCELLENT"]
    elif "fair" in cond or "acceptable" in cond:
        candidates = ["USED_ACCEPTABLE", "PRE_OWNED_FAIR"]
    else:
        candidates = ["USED_GOOD"]

    allowed_ids = get_category_allowed_condition_ids(category_id) if category_id else []
    if allowed_ids:
        allowed_enums = {_CONDITION_ID_TO_ENUM[i] for i in allowed_ids if i in _CONDITION_ID_TO_ENUM}
        for candidate in candidates:
            if candidate in allowed_enums:
                return candidate
        # None of our preferred candidates are allowed here - fall back to whatever
        # this category's own policy does allow, in a low-to-high dubiousness order.
        for candidate in ["USED_EXCELLENT", "PRE_OWNED_EXCELLENT", "NEW_OTHER", "NEW"]:
            if candidate in allowed_enums:
                return candidate
        if allowed_enums:
            return next(iter(allowed_enums))

    return candidates[0]



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


# eBay rejects offer creation with errorId 25002 ("select the package size that
# best fits your item") whenever the account's fulfillment policy includes a
# carrier/service that needs to know package type + weight + dimensions to price
# shipping (e.g. Royal Mail parcel services) and the offer doesn't provide one.
# We don't know the real physical size, so pick a rough preset from a size hint
# (see ebay.py's package-size guess) - good enough to get the draft created; the
# user can correct the real weight/dimensions on eBay's site before publishing.
_PACKAGE_PRESETS = {
    "small": {"weight_kg": 0.3, "dims_cm": (25.0, 20.0, 3.0)},
    "medium": {"weight_kg": 1.0, "dims_cm": (35.0, 25.0, 10.0)},
    "large": {"weight_kg": 3.0, "dims_cm": (45.0, 35.0, 20.0)},
}


def _package_weight_and_size(size_hint: str | None) -> dict:
    preset = _PACKAGE_PRESETS.get((size_hint or "medium").lower(), _PACKAGE_PRESETS["medium"])
    length, width, height = preset["dims_cm"]
    return {
        "packageType": "PARCEL_OR_PADDED_ENVELOPE",
        "weight": {"value": preset["weight_kg"], "unit": "KILOGRAM"},
        "dimensions": {"length": length, "width": width, "height": height, "unit": "CENTIMETER"},
    }


def create_offer(sku: str, category_id: str, price: str, package_size_hint: str | None = None) -> dict:
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
        "packageWeightAndSize": _package_weight_and_size(package_size_hint),
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
