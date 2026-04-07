"""Station logo cache — reads locally cached logos from data/logos/."""
import json
import re
from pathlib import Path

from .config import DATA_DIR

LOGOS_DIR = DATA_DIR / "logos"
LOGOS_INDEX = LOGOS_DIR / "index.json"


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
