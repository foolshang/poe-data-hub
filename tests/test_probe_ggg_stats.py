from unittest.mock import MagicMock

from tools.probe_ggg_stats import VARIANTS, summarize


def _resp(status, ctype, text="", content=b""):
    r = MagicMock(status_code=status, text=text, content=content or text.encode())
    r.headers = {"content-type": ctype, "server": "cloudflare", "cf-ray": "x-FRA", "x-unrelated": "ignored"}
    return r


def test_summarize_keeps_body_only_when_it_is_not_the_json_we_wanted():
    ok = summarize(_resp(200, "application/json", content=b"{}" * 10), 0.5)
    assert ok["status"] == 200 and "body_snippet" not in ok and ok["headers"]["cf-ray"] == "x-FRA" and "x-unrelated" not in ok["headers"]
    bad = summarize(_resp(403, "text/html", text="<html>\n  Just a   moment...</html>"), 1.234)
    assert bad["status"] == 403 and bad["body_snippet"] == "<html> Just a moment...</html>" and bad["elapsed_s"] == 1.23


def test_probe_sends_only_the_honest_user_agent_the_real_refresh_sends():
    from tools.probe_ggg_stats import ENDPOINTS
    from tools import refresh_stats

    assert len(VARIANTS) == 1 and VARIANTS[0][1] == refresh_stats.USER_AGENT          # no variants, no spoofing, no default-UA trick
    assert "@" not in VARIANTS[0][1] or "HUB_CONTACT" in __import__("os").environ
    assert set(ENDPOINTS.values()) == {t.stats_url for t in refresh_stats.TARGETS}
