from pathlib import Path

from src.utils.config import DEFAULT_CONFIG_PATH, resolve_healthcheck_urls


def test_env_var_names_resolve_to_urls():
    cfg = {"healthchecks": {"meta_ping_url_env": "X_META", "news_ping_url_env": "X_NEWS"}}
    resolve_healthcheck_urls(cfg, {"X_META": " https://hc-ping.com/abc \n"})
    assert cfg["healthchecks"]["meta_ping_url"] == "https://hc-ping.com/abc"
    assert cfg["healthchecks"]["news_ping_url"] == ""        # unset -> blank -> that job does not ping


def test_missing_block_is_fine():
    resolve_healthcheck_urls({}, {})


def test_committed_config_holds_no_ping_url():
    text = Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8")
    assert "hc-ping.com" not in text


def test_user_agent_never_contains_an_email_unless_the_environment_gives_one(monkeypatch):
    from src.utils.http_client import PROJECT_URL, build_user_agent

    monkeypatch.delenv("HUB_CONTACT", raising=False)
    assert build_user_agent("p/1") == f"p/1 (contact: {PROJECT_URL})"
    monkeypatch.setenv("HUB_CONTACT", "ops@example.org")
    assert build_user_agent("p/1") == "p/1 (contact: ops@example.org)"


def test_no_personal_address_in_tracked_python_sources():
    root = Path(DEFAULT_CONFIG_PATH).parents[1]
    offenders = [str(p.relative_to(root)) for d in ("src", "tools") for p in (root / d).rglob("*.py")
                 if "foolshang.live" "@gmail.com" in p.read_text(encoding="utf-8")]
    assert not offenders, offenders
