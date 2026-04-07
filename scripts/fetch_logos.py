#!/usr/bin/env python3
"""
Fetch and cache Australian radio station logos from the Fandom wiki.

Run this standalone before or after scanning to populate the logo cache.
The Dabio app will then serve these logos for station cards and Now Playing.

Usage:
    python scripts/fetch_logos.py [--size 200] [--force]

Options:
    --size N    Resize logos to NxN pixels (default: 200)
    --force     Re-download even if cache already exists
"""
import argparse
import asyncio
import json
import re
import sys
from io import BytesIO
from pathlib import Path

try:
    import httpx
except ImportError:
    sys.exit("httpx is required: pip install httpx")

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGOS_DIR = DATA_DIR / "logos"
LOGOS_INDEX = LOGOS_DIR / "index.json"
WIKI_API = "https://logos.fandom.com/api.php"
CATEGORY = "Category:Radio_stations_in_Australia"


def normalize(name: str) -> str:
    return re.sub(r'[^a-z0-9]', '', name.lower())


async def get_category_pages(client: httpx.AsyncClient) -> list[str]:
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
            print(f"  API error: HTTP {resp.status_code}")
            break

        data = resp.json()
        for member in data.get("query", {}).get("categorymembers", []):
            pages.append(member["title"])

        if "continue" in data:
            cmcontinue = data["continue"].get("cmcontinue")
        else:
            break

    return pages


async def get_page_image(client: httpx.AsyncClient, title: str) -> str | None:
    """Get the main image URL for a wiki page."""
    # Try pageimages prop first (main/infobox image)
    params = {
        "action": "query",
        "titles": title,
        "prop": "pageimages",
        "format": "json",
        "pithumbsize": "400",
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

    # Fallback: get images listed on the page
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
                return await get_image_url(client, img_title)

    return None


async def get_image_url(client: httpx.AsyncClient, file_title: str) -> str | None:
    """Get the direct URL for a wiki file."""
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url",
        "iiurlwidth": "400",
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


def get_extension(url: str) -> str:
    path = url.split("?")[0].split("#")[0].lower()
    for ext in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        if ext in path:
            return ext
    return '.png'


def resize_image(image_bytes: bytes, size: int) -> bytes | None:
    """Resize image to fit within size x size, preserving aspect ratio.

    Returns PNG bytes, or None if PIL is not available.
    """
    if not HAS_PIL:
        return None

    try:
        img = Image.open(BytesIO(image_bytes))
        # Convert to RGBA for consistent handling
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGBA')
        img.thumbnail((size, size), Image.LANCZOS)
        out = BytesIO()
        img.save(out, format='PNG', optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"    Resize failed: {e}")
        return None


async def fetch_all_logos(size: int = 200, force: bool = False) -> int:
    LOGOS_DIR.mkdir(parents=True, exist_ok=True)

    if not force and LOGOS_INDEX.exists():
        with open(LOGOS_INDEX) as f:
            existing = json.load(f)
        if existing:
            print(f"Cache already has {len(existing)} logos. Use --force to re-download.")
            return len(existing)

    print(f"Fetching station logos from Fandom wiki...")
    if HAS_PIL:
        print(f"Pillow detected — resizing to {size}x{size}px")
    else:
        print("Pillow not installed — saving at original size (pip install Pillow to resize)")

    index: dict[str, str] = {}
    count = 0
    failed = 0

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        pages = await get_category_pages(client)
        print(f"Found {len(pages)} station pages\n")

        for i, title in enumerate(pages, 1):
            try:
                image_url = await get_page_image(client, title)
                if not image_url:
                    print(f"  [{i}/{len(pages)}] {title}: no image found")
                    failed += 1
                    continue

                resp = await client.get(image_url)
                if resp.status_code != 200 or len(resp.content) < 100:
                    print(f"  [{i}/{len(pages)}] {title}: download failed")
                    failed += 1
                    continue

                image_data = resp.content
                safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', title)

                # Resize if PIL available
                resized = resize_image(image_data, size)
                if resized:
                    filename = f"{safe_name}.png"
                    (LOGOS_DIR / filename).write_bytes(resized)
                else:
                    ext = get_extension(image_url)
                    filename = f"{safe_name}{ext}"
                    (LOGOS_DIR / filename).write_bytes(image_data)

                norm = normalize(title)
                index[norm] = filename
                count += 1
                print(f"  [{i}/{len(pages)}] {title} -> {filename}")

            except Exception as e:
                print(f"  [{i}/{len(pages)}] {title}: error - {e}")
                failed += 1

    # Save index
    with open(LOGOS_INDEX, "w") as f:
        json.dump(index, f, indent=2)

    print(f"\nDone: {count} logos cached, {failed} failed")
    print(f"Saved to: {LOGOS_DIR}")
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Australian radio station logos from Fandom wiki"
    )
    parser.add_argument("--size", type=int, default=200,
                        help="Resize logos to NxN pixels (default: 200)")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if cache exists")
    args = parser.parse_args()

    count = asyncio.run(fetch_all_logos(size=args.size, force=args.force))
    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
