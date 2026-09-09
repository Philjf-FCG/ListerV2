import re
import httpx
from PIL import Image
import io

r = httpx.get("https://www.vinted.co.uk/items/9857536661", headers={"User-Agent": "Mozilla/5.0"})
print("Status:", r.status_code)
urls = set(re.findall(r"https://images\d*\.vinted\.net/t/[^\s\"']+", r.text))
for u in urls:
    r_img = httpx.get(u)
    if r_img.status_code == 200:
        try:
            img = Image.open(io.BytesIO(r_img.content))
            print(f"URL: {u[:80]}... -> Size: {img.size}")
        except Exception:
            pass
