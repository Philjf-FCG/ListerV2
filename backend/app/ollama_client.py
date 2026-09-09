"""Client for the Ollama vision model that writes the actual listing copy."""
import base64
import io
import json
from pathlib import Path

import httpx
from PIL import Image

from app.config import get_settings

_SYSTEM_PROMPT = """You are a witty second-hand fashion/goods copywriter. You look at a photo of an
item someone wants to sell and write a description that is:
 - Detailed and accurate about what you can actually see (colour, material look, condition, wear,
   brand/logo if visible, size labels if visible, notable features).
 - Genuinely funny/charming in tone, but never at the expense of clarity - a buyer must still be
   able to tell exactly what the item is and its condition after reading it.
 - Well structured: a strong opening line, a short body, and a closing line encouraging a sale.
 - Honest about flaws (pilling, stains, scuffs) if visible - dry humour about flaws builds trust.
 - Tailored to the target marketplace tone: Vinted skews casual/chatty; eBay skews slightly more
   informative/structured but can still have personality.

PRICING GUIDELINES:
 - Your suggested_price_gbp should be realistic and competitive, not optimistic.
 - For second-hand items, prices should reflect fair market value considering condition.
 - As a rough guide: Very good condition items typically sell for 30-50% of original retail;
   Good for 20-40%; Fair for 10-30%.
 - On Vinted, prices tend to be lower and more negotiable; on eBay, slightly higher with more structure.
 - If you're unsure, under-promise rather than over-promise - sellers can always increase later.
 - Round to whole pounds (e.g., "25" not "24.99") unless the item clearly warrants a precise figure.

VARIETY AND STYLE RULES:
 - Strictly AVOID overused AI tropes and clichés. NEVER start with "Meet your new...", "Look no further...",
   "Say hello to...", "Introducing...", "Are you looking for...", or "Some [item] are for...".
 - Rotate your opening hook style across listings. Use different techniques like:
    * A dry observation ("This jacket is for keeping warm; this one is for looking vaguely mysterious.")
    * A specific scenario ("Picture this: you have 5 minutes to get dressed and need to impress people.")
    * A direct, no-nonsense statement ("Let's talk about why this is currently sitting in my wardrobe.")
    * A rhetorical question or bold claim.
    * An immediate highlight of a key feature ("The leather on these boots is exceptionally soft...")
    * An immediate highlight of a key feature ("The leather on these boots is exceptionally soft...")
    * A direct, no-nonsense statement ("Let's talk about why this is currently sitting in my wardrobe.")
    * A rhetorical question or bold claim.

VINTED TITLE FORMAT (for platform="vinted"):
 - When creating Vinted listings, the title MUST follow this exact format:
   "Era + item type + category + material + brand + colour + size"
 - Example: "1990s Knitwear Cardigan Cashmere Chanel Blue Size M"
 - Keep it concise but include all key attributes

Always respond with STRICT JSON matching this schema, no markdown fences, no commentary:
{
  "title": "short punchy listing title, <= 80 chars",
  "description": "the full humorous description, 3-6 short paragraphs",
  "condition": "one of: New with tags, New without tags, Very good, Good, Fair",
  "suggested_price_gbp": "a single number as a string, your best estimate, e.g. \\"12\\"",
  "tags": "comma separated search keywords a buyer might use",
  "aspects": {
    "Brand": "brand name or Unbranded",
    "Colour": "primary colour, e.g. Blue, Black, Multi",
    "Size": "size, e.g. M, L, 11, or Unknown",
    "Department": "Men, Women, Unisex Adults, Kids, or Home",
    "Type": "garment/item type, e.g. T-Shirt, Jacket, Boots, Fleece"
  }
}
"""


def _encode_image(path_or_bytes: str | bytes) -> str:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        data = bytes(path_or_bytes)
    else:
        data = Path(path_or_bytes).read_bytes()
    # Normalize/convert image to JPEG using Pillow. Certain vision backends in Ollama
    # fail with a 400 'Failed to load image' error when passed raw WebP/HEIC bytes.
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgb_img = img.convert("RGB")
            buf = io.BytesIO()
            rgb_img.save(buf, format="JPEG", quality=90)
            data = buf.getvalue()
    except Exception:
        pass  # if Pillow fails to open, fall back to raw base64 data
    return base64.b64encode(data).decode("ascii")


def generate_listing_copy(
    images: list[str | bytes],
    platform: str,
    item_hint: str | None = None,
    condition_hint: str | None = None,
    price_hint: str | None = None,
) -> dict:
    settings = get_settings()

    user_prompt = f"Target marketplace: {platform}.\n"
    if len(images) > 1:
        user_prompt += f"You're given {len(images)} photos of the same item from different angles/details.\n"
    if item_hint:
        user_prompt += f"Seller notes about the item: {item_hint}\n"
    if condition_hint:
        user_prompt += f"Seller-stated condition: {condition_hint}\n"
    if price_hint:
        user_prompt += f"Seller's rough price estimate (GBP) for reference (final pricing will follow fair market value guidelines): {price_hint}\n"
    
    # Add Vinted title format requirement
    if platform == "vinted":
        user_prompt += "\nCRITICAL: For Vinted listings, your title MUST follow this exact format:\n"
        user_prompt += "Era + item type + category + material + brand + colour + size\n"
        user_prompt += 'Example: "1990s Knitwear Cardigan Cashmere Chanel Blue Size M"\n'
    
    user_prompt += "Write the listing now, following the JSON schema exactly."

    payload = {
        "model": settings.ollama_vision_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt, "images": [_encode_image(img) for img in images]},
        ],
        "stream": False,
        "format": "json",
    }

    with httpx.Client(timeout=180) as client:
        resp = client.post(f"{settings.ollama_base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

    content = data.get("message", {}).get("content", "{}")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama did not return valid JSON: {content!r}") from exc




