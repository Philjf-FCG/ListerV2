import json
import logging
import mimetypes
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.db import get_connection
import app.ebay_client as ebay_client
from app.photo_sources import google_photos, local
from app.schemas import GenerateRequest, ListingOut, ListingUpdate, PhotoRef

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ebay", tags=["ebay"])

# Placeholder photo for eBay listings when no photos are available
PLACEHOLDER_PHOTO_PATH = Path(__file__).parent.parent / "static" / "placeholder_photo.jpg"

def _ensure_placeholder_photo_exists():
    """Ensure placeholder photo exists in the static directory."""
    if not PLACEHOLDER_PHOTO_PATH.exists():
        # Create a simple placeholder image (red background with text)
        from PIL import Image, ImageDraw, ImageFont
        try:
            # Create a 800x600 red image with "NO PHOTO" text
            img = Image.new('RGB', (800, 600), color='red')
            draw = ImageDraw.Draw(img)
            
            # Try to use default font or fallback to basic one
            try:
                font = ImageFont.truetype("arial.ttf", 48)
            except:
                font = ImageFont.load_default()
                
            text = "NO PHOTO AVAILABLE"
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
            
            # Center the text
            x = (800 - text_width) // 2
            y = (600 - text_height) // 2
            
            draw.text((x, y), text, fill=(255, 255, 255), font=font)
            img.save(PLACEHOLDER_PHOTO_PATH, "JPEG", quality=85)
        except Exception as e:
            logger.error(f"Failed to create placeholder photo: {e}")

@router.get("/status")
def ebay_status():
    return {"connected": ebay_client.is_connected()}


@router.get("/category-suggestions")
def category_suggestions(q: str):
    try:
        return ebay_client.get_category_suggestions(q)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _infer_aspect_value(aspect_name: str, title: str, desc: str, tags: str) -> list[str]:
    combined = f"{title} {desc} {tags}".lower()
    name_lower = aspect_name.lower()

    if "brand" in name_lower:
        for brand in [
            "nike", "adidas", "ted baker", "levi's", "levis", "puma", "under armour",
            "the north face", "north face", "easy", "gelert", "tecnic", "technic",
            "superdry", "slazenger", "craghoppers", "manfinity", "u.s. athletic",
            "dartington", "sony", "microsoft", "nintendo", "zara", "h&m"
        ]:
            if brand in combined:
                return [brand.title()]
        return ["Unbranded"]

    if "colour" in name_lower or "color" in name_lower:
        for col in ["black", "blue", "red", "green", "white", "grey", "gray", "brown", "navy", "yellow", "pink", "purple", "orange", "beige", "gold", "silver", "turquoise", "maroon", "olive", "violet", "indigo"]:
            if col in combined:
                return [col.title()]
        return ["Multi"]

    if name_lower == "size" or name_lower == "size:":
        for sz in ["xxl", "xl", "large", "medium", "small", "xs", "l", "m", "s", "12", "11", "10", "9", "8", "7", "6"]:
            if f" {sz} " in f" {combined} " or f"size {sz}" in combined or f"size: {sz}" in combined:
                return [sz.upper() if len(sz) <= 3 else sz.title()]
        return ["M"]

    if "size type" in name_lower:
        if "big" in combined or "tall" in combined or "plus" in combined:
            return ["Big & Tall"]
        return ["Regular"]

    if "fit" in name_lower:
        for fit in ["slim", "relaxed", "athletic", "oversized", "loose", "regular", "classic"]:
            if fit in combined:
                return [fit.title()]
        return ["Regular"]

    if "department" in name_lower:
        if "women" in combined or "ladies" in combined or "female" in combined:
            return ["Women"]
        if "men" in combined or "male" in combined or "mens" in combined or "men's" in combined:
            return ["Men"]
        if "boy" in combined or "girl" in combined or "child" in combined or "kid" in combined:
            return ["Unisex Kids"]
        return ["Unisex Adults"]

    if "type" in name_lower:
        for t in ["t-shirt", "tee", "jacket", "coat", "boots", "shoes", "fleece", "hoodie", "jumper", "trousers", "shorts", "shirt", "polo", "tank", "dress", "skirt", "pants", "leggings"]:
            if t in combined:
                return [t.title()]
        # If we can't determine a specific type, at least provide something generic
        if "jacket" in combined or "coat" in combined:
            return ["Jacket"]
        elif "shirt" in combined or "tee" in combined or "top" in combined:
            return ["Shirt"]
        elif "trousers" in combined or "pants" in combined or "jeans" in combined:
            return ["Pants"]
        elif "shoes" in combined or "boot" in combined:
            return ["Shoes"]
        # Default to a reasonable fallback that eBay will accept
        return ["Clothing"]

    if "package size" in name_lower or "package_size" in name_lower:
        # eBay requires package size for shipping - default to Small Parcel for most clothing items
        if "large" in combined or "big" in combined or "oversized" in combined:
            return ["Large Parcel"]
        elif "medium" in combined or "regular" in combined:
            return ["Medium Parcel"]
        # Default to Small Parcel for most clothing items when no size information is found
        return ["Small Parcel"]

    if "sleeve length" in name_lower or "sleeve" in name_lower:
        if "long sleeve" in combined:
            return ["Long Sleeve"]
        if "sleeveless" in combined or "tank" in combined:
            return ["Sleeveless"]
        return ["Short Sleeve"]

    if "material" in name_lower or "fabric" in name_lower:
        for mat in ["cotton", "polyester", "fleece", "leather", "denim", "wool", "silk", "linen", "nylon"]:
            if mat in combined:
                return [mat.title()]
        return ["Cotton Blend"]

    return ["Unspecified"]


@router.post("/listings/{listing_id}/push")
def push_listing_to_ebay(listing_id: int, category_id: str):
    """Creates a real eBay draft (an unpublished offer) for this listing via the
    official Sell API, ensuring all category-required aspects and valid condition are populated."""
    
    # Ensure placeholder photo exists
    _ensure_placeholder_photo_exists()
    
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    if row["platform"] != "ebay":
        raise HTTPException(status_code=400, detail="This listing isn't targeted at eBay")

    # The eBay SKU is generated once and persisted - never derived from this row's
    # local id (see ebay_client.generate_ebay_sku for why that used to be unsafe).
    sku = row["ebay_sku"]
    if not sku:
        sku = ebay_client.generate_ebay_sku()
        with get_connection() as conn:
            conn.execute("UPDATE listings SET ebay_sku = ? WHERE id = ?", (sku, listing_id))

    photo_items = json.loads(row["photo_items"] or "[]")

    # Listings imported from the Vinted sync feature (section 3) sometimes predate
    # the local photo cache, or had it cleared - top up from the Vinted item's
    # cached photos (see sync.get_local_vinted_photos) if we're otherwise empty-handed.
    if not photo_items:
        with get_connection() as conn:
            vinted_row = conn.execute(
                "SELECT url FROM vinted_items WHERE imported_listing_id = ?", (listing_id,)
            ).fetchone()
        if vinted_row:
            from app.routers.sync import get_local_vinted_photos

            local_photos = get_local_vinted_photos(vinted_row["url"])
            if local_photos:
                logger.info(
                    "Listing %s had no photo_items - found %d cached Vinted photo(s) via %s",
                    listing_id, len(local_photos), vinted_row["url"],
                )
                photo_items = [{"source": "local", "ref": str(p)} for p in local_photos]

    # Extract public image URLs / upload local photos to eBay Picture Services (EPS)
    image_urls = []
    for item in photo_items:
        src = item.get("source")
        ref = item.get("ref", "")
        if src == "local" or (src is None and Path(ref).exists()):
            try:
                eps_url = ebay_client.upload_picture_to_ebay(ref)
                image_urls.append(eps_url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not upload local photo %s to eBay EPS: %s", ref, exc)
                # Continue without this photo rather than failing completely
        elif src == "url" or ref.startswith("http://") or ref.startswith("https://"):
            image_urls.append(ref)
        elif src == "google_photos":
            g_url = google_photos.get_cached_base_url(ref)
            if g_url:
                image_urls.append(g_url)

    # If no images were successfully uploaded, fall back to a visible placeholder
    # so the push still completes - but flag it clearly (see used_placeholder below)
    # so the listing card warns the user rather than silently shipping a fake photo.
    used_placeholder = False
    if not image_urls and PLACEHOLDER_PHOTO_PATH.exists():
        try:
            logger.warning("No real photos available for listing %s - using placeholder", listing_id)
            eps_url = ebay_client.upload_picture_to_ebay(PLACEHOLDER_PHOTO_PATH)
            image_urls.append(eps_url)
            used_placeholder = True
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not upload placeholder photo to eBay EPS: %s", exc)
            logger.warning("No photos available for listing %s (including placeholder)", listing_id)

    title = row["title"] or ""
    desc = row["description"] or ""
    tags = row["tags"] or ""

    # Fetch required category aspects from Taxonomy API and auto-fill them
    aspects = {}
    try:
        req_aspects = ebay_client.get_category_required_aspects(category_id)
        for req in req_aspects:
            aspects[req] = _infer_aspect_value(req, title, desc, tags)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not fetch category aspects for %s: %s", category_id, exc)
        # Continue without aspects - this is not critical for listing creation
        pass

    # eBay requires a package weight/size on the offer for carriers that price
    # shipping by parcel size (see create_offer) - reuse the same "Package Size"
    # text heuristic used for the item aspect above, just mapped to small/medium/large.
    package_size_hint = _infer_aspect_value("Package Size", title, desc, tags)[0].split()[0].lower()

    try:
        ebay_client.create_or_replace_inventory_item(
            sku=sku,
            title=title,
            description=desc,
            condition=row["condition"] or "",
            category_id=category_id,
            image_urls=image_urls if image_urls else None,
            aspects=aspects if aspects else None,
        )
        offer = ebay_client.create_offer(
            sku=sku, category_id=category_id, price=row["price"] or "", package_size_hint=package_size_hint
        )
    except Exception as exc:  # noqa: BLE001
        # Log the specific error for debugging
        logger.error("eBay API error during push: %s", exc)
        with get_connection() as conn:
            conn.execute(
                "UPDATE listings SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?",
                (str(exc), listing_id),
            )
        # Provide more specific error information for the user
        if hasattr(exc, 'response') and exc.response is not None:
            try:
                error_detail = exc.response.json()
                if 'error' in error_detail and 'error_description' in error_detail:
                    raise HTTPException(status_code=502, detail=f"eBay API error {error_detail['error']}: {error_detail['error_description']}") from exc
            except Exception:
                pass  # Fall back to generic error handling
        raise HTTPException(status_code=502, detail=f"eBay API error: {exc}") from exc

    offer_id = offer.get("offerId")
    error_note = f"offer_id:{offer_id}"
    if used_placeholder:
        error_note += ";no_real_photos"
    with get_connection() as conn:
        conn.execute(
            """UPDATE listings SET status = 'posted_as_draft', error = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (error_note, listing_id),
        )
    return {"status": "draft_created", "offerId": offer_id, "sku": sku, "usedPlaceholderPhoto": used_placeholder}


@router.post("/listings/{listing_id}/publish")
def publish_listing_to_ebay(listing_id: int):
    """Publishes an existing inventory draft offer straight to live eBay with 1 click!"""
    with get_connection() as conn:
        row = conn.execute("SELECT ebay_sku FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    sku = row["ebay_sku"]
    if not sku:
        raise HTTPException(status_code=400, detail="No eBay offer found for this listing. Push to eBay first.")
    try:
        # Find offer ID associated with SKU
        headers = ebay_client._user_auth_header()
        resp = httpx.get(f"https://api.ebay.com/sell/inventory/v1/offer?sku={sku}", headers=headers)
        ebay_client._raise_with_ebay_detail(resp)
        offers = resp.json().get("offers", [])
        if not offers:
            raise RuntimeError("No eBay offer found for this listing. Push to eBay first.")
        offer_id = offers[0]["offerId"]
        result = ebay_client.publish_offer(offer_id)
        ebay_item_id = result.get("listingId")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    with get_connection() as conn:
        conn.execute(
            """UPDATE listings SET status = 'posted_as_draft', error = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (f"published:{ebay_item_id}", listing_id),
        )
    return {
        "status": "published",
        "listingId": ebay_item_id,
        "itemUrl": f"https://www.ebay.co.uk/itm/{ebay_item_id}",
    }
