"""Station logo fetcher and cache — scrapes logos from Fandom wiki."""
import json
import re
from pathlib import Path

import httpx

from .config import DATA_DIR
from .logging_config import get_logger

log = get_logger("logos")

LOGOS_DIR = DATA_DIR / "logos"
LOGOS_INDEX = LOGOS_DIR / "index.json"
WIKI_API = "https://logos.fandom.com/api.php"
CATEGORY = "Category:Radio_stations_in_Australia"


def _normalize(name: str) -> str:
    """Normalize a station name for matching."""
    return re.sub(r'[^a-z0-9]', '', name.lower())


def get_logo_index() -> dict[str, str]:
    """Load the cached logo index: {normalized_name: filename}."""
    if LOGOS_INDEX.exists():
        try:
            with open(LOGOS_INDEX) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def find_logo(station_name: str) -> Path | None:
    """Find a cached logo for a station name, using fuzzy matching."""
    index = get_logo_index()
    if not index:
        return None

    norm = _normalize(station_name)

    # Direct match
    if norm in index:
        p = LOGOS_DIR / index[norm]
        if p.exists():
            return p

    # Try matching without common suffixes like "dab+", "fm", "am"
    clean = re.sub(r'(dab\+?|fm|am|digital|radio)$', '', norm).strip()
    if clean and clean in index:
        p = LOGOS_DIR / index[clean]
        if p.exists():
            return p

    # Substring match — wiki name contains station name or vice versa
    for key, filename in index.items():
        if len(norm) >= 3 and (norm in key or key in norm):
            p = LOGOS_DIR / filename
            if p.exists():
                return p
        # Also try the cleaned version
        if clean and len(clean) >= 3 and (clean in key or key in clean):
            p = LOGOS_DIR / filename
            if p.exists():
                return p

    return None


def has_cached_logos() -> bool:
    """Check if logos have already been fetched."""
    if not LOGOS_INDEX.exists():
        return False
    index = get_logo_index()
    return len(index) > 0


async def fetch_all_logos() -> int:
    """Fetch all Australian radio station logos from the Fandom wiki.

    Returns the number of logos downloaded.
    """
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)

    # If we already have logos, skip
    if has_cached_logos():
        index = get_logo_index()
        log.info(f"Logo cache already exists with {len(index)} entries")
        return len(index)

    log.info("Fetching Australian radio station logos from Fandom wiki...")

    index: dict[str, str] = {}
    count = 0

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # Get all pages in the category
            pages = await _get_category_pages(client)
            log.info(f"Found {len(pages)} station pages on wiki")

            for title in pages:
                try:
                    image_url = await _get_page_image(client, title)
                    if not image_url:
                        continue

                    # Download the image
                    ext = _get_extension(image_url)
                    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', title)
                    filename = f"{safe_name}{ext}"
                    filepath = LOGOS_DIR / filename

                    resp = await client.get(image_url)
                    if resp.status_code == 200 and len(resp.content) > 100:
                        filepath.write_bytes(resp.content)
                        norm = _normalize(title)
                        index[norm] = filename
                        count += 1
                        log.debug(f"Cached logo: {title} -> {filename}")
                except Exception as e:
                    log.debug(f"Failed to fetch logo for '{title}': {e}")
                    continue

            # Save index
            with open(LOGOS_INDEX, "w") as f:
                json.dump(index, f, indent=2)

            log.info(f"Cached {count} station logos")
    except Exception as e:
        log.error(f"Logo fetch failed: {e}")

    return count


async def _get_category_pages(client: httpx.AsyncClient) -> list[str]:
    """Get all page titles in the radio stations category."""
    pages: list[str] = []
    cmcontinue = None

    while True:
        params: dict[str, str] = {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": CATEGORY,
            "cmlimit": "500",
            "cmtype": "page",
            "format": "json",
        }
        if cmcontinue:
            params["cmcontinue"] = cmcontinue

        resp = await client.get(WIKI_API, params=params)
        if resp.status_code != 200:
            break

        data = resp.json()
        for member in data.get("query", {}).get("categorymembers", []):
            pages.append(member["title"])

        if "continue" in data:
            cmcontinue = data["continue"].get("cmcontinue")
        else:
            break

    return pages


async def _get_page_image(client: httpx.AsyncClient, title: str) -> str | None:
    """Get the main image URL for a wiki page."""
    # Try pageimages prop first (gets the main/infobox image)
    params = {
        "action": "query",
        "titles": title,
        "prop": "pageimages",
        "format": "json",
        "pithumbsize": "200",
    }

    resp = await client.get(WIKI_API, params=params)
    if resp.status_code != 200:
        return None

    data = resp.json()
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        thumb = page.get("thumbnail", {}).get("source")
        if thumb:
            return thumb
        original = page.get("original", {}).get("source")
        if original:
            return original

    # Fallback: get images from the page and use the first valid one
    params = {
        "action": "query",
        "titles": title,
        "prop": "images",
        "format": "json",
    }
    resp = await client.get(WIKI_API, params=params)
    if resp.status_code != 200:
        return None

    data = resp.json()
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        images = page.get("images", [])
        for img in images:
            img_title = img.get("title", "")
            if any(img_title.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.svg']):
                return await _get_image_url(client, img_title)

    return None


async def _get_image_url(client: httpx.AsyncClient, file_title: str) -> str | None:
    """Get the direct URL for a wiki file."""
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "200",
        "format": "json",
    }
    resp = await client.get(WIKI_API, params=params)
    if resp.status_code != 200:
        return None

    data = resp.json()
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        imageinfo = page.get("imageinfo", [])
        if imageinfo:
            return imageinfo[0].get("thumburl") or imageinfo[0].get("url")
    return None


def _get_extension(url: str) -> str:
    """Extract file extension from URL."""
    path = url.split("?")[0].split("#")[0].lower()
    for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        if ext in path:
            return ext
    return '.png'
