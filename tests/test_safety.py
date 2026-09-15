"""The never-submit rule, secret redaction, and robots.txt handling."""
from pathlib import Path

import pytest
import requests

import watch

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_post_to_islamweb_raises_instead_of_being_sent():
    session = watch.ReadOnlyIslamwebSession()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(watch.NeverSubmitError):
            session.request(method, watch.FATWA_PAGE_URL, data={"question": "x"})


def test_the_guard_covers_subdomains_and_the_convenience_helpers():
    session = watch.ReadOnlyIslamwebSession()
    with pytest.raises(watch.NeverSubmitError):
        session.post("https://www.islamweb.net/ar/fatwa/ask/send", data={})
    with pytest.raises(watch.NeverSubmitError):
        session.post("https://anything.islamweb.net/submit", data={})


def test_the_source_contains_no_post_to_islamweb():
    source = Path(watch.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "requests.post(%s" % watch.FATWA_PAGE_URL not in lowered
    # The only POST in the file is the Telegram sendMessage call.
    post_lines = [ln.strip() for ln in source.splitlines() if "requests.post" in ln
                  or ".post(" in ln]
    for line in post_lines:
        assert "islamweb" not in line.lower() or "raises" in line.lower()


def test_user_agent_names_the_repo_so_operators_can_reach_a_human():
    assert "islamweb-watcher" in watch.USER_AGENT
    assert "github.com" in watch.USER_AGENT
    assert "never submits" in watch.USER_AGENT.lower()


def test_poll_interval_floor_is_polite():
    assert watch.POLL_INTERVAL_SECONDS >= 20


def test_secrets_are_redacted_before_logging(monkeypatch):
    monkeypatch.setenv("CALLMEBOT_APIKEY", "supersecret123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "9999:abcdefghijkl")
    message = "calling https://api.callmebot.com/whatsapp.php?apikey=supersecret123"
    redacted = watch.redact(message)
    assert "supersecret123" not in redacted
    assert watch.redact("token 9999:abcdefghijkl").find("abcdefghijkl") == -1


def test_robots_allowing_the_page():
    text = (FIXTURES / "robots_allowed.txt").read_text(encoding="utf-8")
    verdict = watch.robots_verdict(text, watch.FATWA_PAGE_PATH)
    assert verdict["allowed"] is True
    assert verdict["crawl_delay"] == 10


def test_robots_disallowing_the_fatwa_directory():
    text = (FIXTURES / "robots_disallowed.txt").read_text(encoding="utf-8")
    assert watch.robots_verdict(text, "/ar/fatwa/")["allowed"] is False
    assert watch.robots_verdict(text, watch.FATWA_PAGE_PATH)["allowed"] is False
    # A more specific agent group must not be used for the generic check.
    assert watch.robots_verdict(text, "/ar/fatwa/", agent="Googlebot")["allowed"] is True


def test_longest_match_wins_and_allow_breaks_ties():
    text = "User-agent: *\nDisallow: /ar/\nAllow: /ar/fatwa/\n"
    assert watch.robots_verdict(text, "/ar/fatwa/x")["allowed"] is True
    assert watch.robots_verdict(text, "/ar/other")["allowed"] is False


def test_an_empty_disallow_value_permits_everything():
    assert watch.robots_verdict("User-agent: *\nDisallow:\n", "/ar/fatwa/")["allowed"] is True


def test_wildcards_are_honoured():
    text = "User-agent: *\nDisallow: /*/fatwa/\n"
    assert watch.robots_verdict(text, "/ar/fatwa/page")["allowed"] is False


def test_unreachable_robots_is_reported_as_unknown_not_as_permission(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            raise requests.ConnectionError("no route")

    verdict = watch.check_robots(Boom())
    assert verdict["allowed"] is None
