"""Parse Vinted personal data export HTML file."""
import re
from pathlib import Path
from bs4 import BeautifulSoup

from app.schemas import VintedItemIn


def parse_vinted_export(html_path: str | Path) -> list[VintedItemIn]:
    """Parse Vinted listings export HTML and return items with all photos."""
    html_path = Path(html_path)
    
    if not html_path.exists():
        raise FileNotFoundError(f"Vinted export not found: {html_path}")
    
    soup = BeautifulSoup(html_path.read_text(), "html.parser")
    
    # Find the 'Listings' section header
    listings_header = soup.find(string=lambda t: t and "Listings" in str(t))
    if not listings_header:
        raise ValueError("Could not find Listings section in export")
    
    # Get parent cell, then find all subsequent item cells
    listings_cell = listings_header.find_parent("div", class_="cell")
    if not listings_cell:
        raise ValueError("Could not find Listings cell")
    
    items: list[VintedItemIn] = []
    
    # Find all item cells that come after the listings header
    current = listings_cell.find_next_sibling()
    while current:
        # Check if this is an item cell (has item_photo elements)
        photo_elements = current.select('[itemprop="item_photo"]')
        
        if photo_elements:
            # Parse item details
            title_elem = current.select_one('[itemprop="title"]')
            desc_elem = current.select_one('[itemprop="description"]')
            price_elem = current.find("span", attrs={"itemprop": "order_value"})
            
            title = title_elem.get_text(strip=True) if title_elem else ""
            description = desc_elem.get_text(strip=True, separator="\n") if desc_elem else ""
            price_text = price_elem.get_text(strip=True) if price_elem else ""
            
            # Parse price to just the number (e.g., "12.0 GBP" -> "12.0")
            price_match = re.search(r"([\d.,]+)", price_text)
            price = price_match.group(1) if price_match else None
            
            # Extract photos - both href and src might be different sizes
            photo_urls: list[str] = []
            seen_urls = set()
            
            item_id = None
            for photo_elem in photo_elements:
                img = photo_elem.find_parent("a")
                if img and img.get("href"):
                    url = img["href"]
                    # Convert relative URL to absolute (photos are in same folder structure)
                    if not url.startswith(("http", "data:")):
                        url = f"file://{html_path.parent / url}"
                    
                    # Extract item ID from photo path for the main Vinted URL
                    if item_id is None:
                        id_match = re.search(r"(\d{10})", url)
                        if id_match:
                            item_id = id_match.group(1)
                    
                    if url not in seen_urls:
                        photo_urls.append(url)
                        seen_urls.add(url)
            
            # Build the main Vinted item URL from extracted item ID
            vinted_url = f"https://www.vinted.co.uk/items/{item_id}" if item_id else ""
            
            items.append(VintedItemIn(
                url=vinted_url,
                title=title,
                price=price,
                photo_urls=photo_urls
            ))
        
        # Move to next sibling
        current = current.find_next_sibling()
    
    return items
