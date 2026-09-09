import json
from pathlib import Path
from fastapi import APIRouter, HTTPException

from app import ebay_client
from app.db import get_connection
from app.photo_sources import google_photos, local
from app.routers.sync import get_local_vinted_photos

router = APIRouter(prefix="/ebay", tags=["ebay"])


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
        for col in ["black", "blue", "red", "green", "white", "grey", "gray", "brown", "navy", "yellow", "pink", "purple", "orange"]:
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
        for t in ["t-shirt", "tee", "jacket", "coat", "boots", "shoes", "fleece", "hoodie", "jumper", "trousers", "shorts", "shirt", "polo", "tank"]:
            if t in combined:
                return [t.title()]
        return ["T-Shirt"]

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
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    if row["platform"] != "ebay":
        raise HTTPException(status_code=400, detail="This listing isn't targeted at eBay")

    # Check if there are local photos available directly or via linked Vinted item
    photo_items = json.loads(row["photo_items"] or "[]")
    
    # If the listing only has a single remote URL, check if local photos exist for this Vinted item
    if not any(item.get("source") == "local" for item in photo_items):
        with get_connection() as conn:
            vinted_row = conn.execute(
                "SELECT url FROM vinted_items WHERE imported_listing_id = ?", (listing_id,)
            ).fetchone()
        if vinted_row:
            local_photos = get_local_vinted_photos(vinted_row["url"])
            if local_photos:
                photo_items = [{"source": "local", "ref": str(p)} for p in local_photos]
                thumbnail_urls = [
                    f"/photos/thumbnail?path={local.ensure_thumbnail(p).name}" for p in local_photos
                ]
                with get_connection() as conn:
                    conn.execute(
                        """UPDATE listings SET photo_items = ?, thumbnail_urls = ?, updated_at = datetime('now')
                           WHERE id = ?""",
                        (json.dumps(photo_items), json.dumps(thumbnail_urls), listing_id),
                    )

    # Extract public image URLs / upload local photos to eBay Picture Services (EPS)
    image_urls = []
    for item in photo_items:
        src = item.get("source")
        ref = item.get("ref", "")
        if src == "local" or (src is None and Path(ref).exists()):
            try:
                eps_url = ebay_client.upload_picture_to_ebay(ref)
                image_urls.append(eps_url)
            except Exception as exc:
                print(f"Warning: could not upload local photo {ref} to eBay EPS: {exc}")
        elif src == "url" or ref.startswith("http://") or ref.startswith("https://"):
            image_urls.append(ref)
        elif src == "google_photos":
            g_url = google_photos.get_cached_base_url(ref)
            if g_url:
                image_urls.append(g_url)

    title = row["title"] or ""
    desc = row["description"] or ""
    tags = row["tags"] or ""

    # Fetch required category aspects from Taxonomy API and auto-fill them
    aspects = {}
    try:
        req_aspects = ebay_client.get_category_required_aspects(category_id)
        for req in req_aspects:
            aspects[req] = _infer_aspect_value(req, title, desc, tags)
    except Exception:
        pass  # best-effort aspect lookup

    sku = f"lister-{listing_id}"
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
        offer = ebay_client.create_offer(sku=sku, category_id=category_id, price=row["price"] or "")
    except RuntimeError as exc:
        with get_connection() as conn:
            conn.execute(
                "UPDATE listings SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?",
                (str(exc), listing_id),
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    offer_id = offer.get("offerId")
    with get_connection() as conn:
        conn.execute(
            """UPDATE listings SET status = 'posted_as_draft', error = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (f"offer_id:{offer_id}", listing_id),
        )
    return {"status": "draft_created", "offerId": offer_id, "sku": sku}


@router.post("/listings/{listing_id}/publish")
def publish_listing_to_ebay(listing_id: int):
    """Publishes an existing inventory draft offer straight to live eBay with 1 click!"""
    sku = f"lister-{listing_id}"
    try:
        # Find offer ID associated with SKU
        headers = ebay_client._user_auth_header()
        import httpx
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
