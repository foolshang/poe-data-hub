"""Fetch PoE1's "Deal of the Day" MTX shop specials (phase 8, ported from
D:\\Projects\\Bot-Poe-Discord\\poe-discord-bot\\deals.py).

GET https://www.pathofexile.com/ -> scrape #newsSidebar .shopSpecialsBox .deal

PoE2 (pathofexile2.com) was surveyed there and has no scrapable Deal of the
Day -- its shop page is a client-rendered SPA with no server-side deal data
or discoverable JSON API. PoE1's homepage sidebar is the only real source,
so this is deliberately not split per-game like prices/news -- there is no
poe2 side to split (see SPEC.md 8.5).

Deliberately NOT ported: seen_deals.json / mark_deals_seen (which deals have
already been *posted* is bot-side state); `first_seen_at` per deal (a
similar but distinct concept -- "when did the hub first see this" purely as
publish metadata) is computed by src/storage/publisher.py's
build_mtx_schema, which merges against the previously-published file, not
here -- this module only returns a stateless snapshot of what's on the page
right now.
"""
from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup

from src.utils.http_client import HttpClient

DEALS_URL = "https://www.pathofexile.com/"
# pathofexile.com returns 403 for a bare "Mozilla/5.0" -- needs a full
# browser-looking UA (same requirement as src/fetchers/news.py's GGG scrape).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}

PERCENT_RE = re.compile(r"(\d+)\s*%")
POINTS_RE = re.compile(r"discounted to (\d+)\s*Points", re.I)


def fetch_deals(http: HttpClient, url: str = DEALS_URL) -> list[dict[str, Any]]:
    text = http.get_text(url, headers=HEADERS)
    soup = BeautifulSoup(text, "html.parser")

    deals: list[dict[str, Any]] = []
    for deal_el in soup.select("#newsSidebar .shopSpecialsBox .deal"):
        name_el = deal_el.select_one("a.name")
        if not name_el:
            continue

        percent_el = deal_el.select_one(".l2")
        percent_match = PERCENT_RE.search(percent_el.get_text(" ", strip=True)) if percent_el else None

        desc_el = deal_el.select_one(".description")
        points_match = POINTS_RE.search(desc_el.get_text(" ", strip=True)) if desc_el else None

        time_el = deal_el.select_one(".time")

        deals.append(
            {
                "item_name": name_el.get_text(strip=True),
                "link": name_el.get("href", ""),
                "discount_percent": int(percent_match.group(1)) if percent_match else None,
                "points_price": int(points_match.group(1)) if points_match else None,
                "ends_in": time_el.get_text(strip=True) if time_el else "",
            }
        )
    return deals
