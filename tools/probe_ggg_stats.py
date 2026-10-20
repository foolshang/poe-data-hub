"""Diagnostic probe: why does GGG's trade(2)/data/stats answer 403 to a GitHub Actions runner on some runs?

2026-10-19: refresh-stats.yml got 200 at 08:21 (old User-Agent, with a contact email) and 08:56 (NEW User-Agent: project
URL, no email) and 403 at 10:47 (new User-Agent) -- same UA, different outcome, so the UA alone is not the cause. This
makes ONE honest request per endpoint (exactly what tools/refresh_stats.py sends: same library, same User-Agent) and
prints what is needed to tell the remaining explanations apart:

  * runner egress IP / organisation / region (ipinfo.io)     -> does the IP change per run, is a bad IP reproducible?
  * status, elapsed, selected response headers, body snippet -> Cloudflare challenge? rate limit? a message from GGG?

Deliberately NOT done (project owner, 2026-10-20): no User-Agent variants, no spoofing, no proxies, no retries -- a
diagnostic must not look like, or be, an attempt to get around a block. Two requests per run, one run per hour, and the
workflow stops itself at a hard deadline (see diagnose-ggg.yml). It publishes nothing and needs no secrets.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any

import requests

from src.utils.http_client import build_user_agent

ENDPOINTS = {
    "poe2": "https://www.pathofexile.com/api/trade2/data/stats",
    "poe1": "https://www.pathofexile.com/api/trade/data/stats",
}
PRODUCT = "poe-data-hub-stats-refresh/0.1"
# (label, User-Agent): the same honest User-Agent the real refresh sends -- one entry on purpose
VARIANTS: list[tuple[str, str | None]] = [("refresh_stats user-agent", build_user_agent(PRODUCT))]
HEADERS_OF_INTEREST = ("server", "cf-ray", "cf-mitigated", "content-type", "retry-after", "x-rate-limit-policy",
                       "x-rate-limit-ip", "x-rate-limit-ip-state", "set-cookie", "cf-cache-status")
SPACING_SECONDS = 4


def summarize(resp: requests.Response, elapsed: float) -> dict[str, Any]:
    """Pure: the fields worth logging from one response (the body only when it is not the JSON we want)."""
    ctype = resp.headers.get("content-type", "")
    ok_json = resp.status_code == 200 and "json" in ctype.lower()
    out: dict[str, Any] = {
        "status": resp.status_code,
        "elapsed_s": round(elapsed, 2),
        "bytes": len(resp.content),
        "headers": {h: resp.headers[h][:120] for h in HEADERS_OF_INTEREST if h in resp.headers},
    }
    if not ok_json:
        out["body_snippet"] = " ".join(resp.text[:400].split())
    return out


def probe_once(url: str, user_agent: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    t0 = time.time()
    try:
        return summarize(requests.get(url, headers=headers, timeout=20), time.time() - t0)
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {e}"}


def runner_identity() -> dict[str, Any]:
    try:
        r = requests.get("https://ipinfo.io/json", timeout=15)
        data = r.json()
        return {k: data.get(k) for k in ("ip", "org", "city", "region", "country")}
    except Exception as e:  # noqa: BLE001 -- diagnostic only
        return {"error": str(e)}


def main() -> int:
    print("runner:", runner_identity(), "| run", os.environ.get("GITHUB_RUN_ID"), "| utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), flush=True)
    for game, url in ENDPOINTS.items():
        for label, ua in VARIANTS:
            print(f"{game} | {label:<28} | {probe_once(url, ua)}", flush=True)
            time.sleep(SPACING_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
