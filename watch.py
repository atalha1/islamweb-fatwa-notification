#!/usr/bin/env python3
# ============================================================================
#  ###  HARD RULE - READ THIS BEFORE EDITING ANYTHING BELOW  ###
#
#  THIS TOOL MUST NEVER SUBMIT THE FATWA FORM.
#
#  It issues GET requests only, to read whether the submission window is
#  open. It NEVER issues a POST/PUT/PATCH/DELETE to islamweb.net, never
#  fills in a form field, never replays a form, and never automates a
#  submission in any way. Its only output is a notification to a human,
#  who then writes and submits the question by hand.
#
#  This is enforced at runtime by ReadOnlyIslamwebSession below, which
#  raises NeverSubmitError on any non-GET request to islamweb.net.
#  Do not remove that class. Do not add an "auto-submit" flag.
# ============================================================================
"""Notification-only watcher for the Islamweb fatwa submission window.

Usage:
    python watch.py --once            single check, prints state, sends nothing
    python watch.py --run             polling run (used by GitHub Actions)
    python watch.py --test-alert      one test message through both channels
    python watch.py --mark-sent ID    mark a queue entry as sent (+ git commit)
    python watch.py --check-robots    print robots.txt verdict for the page
    python watch.py --remind          degraded mode: send next queued question
"""

from __future__ import annotations

import argparse
import csv
import html as html_mod
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import requests

try:
    import yaml
except ImportError:  # pragma: no cover - only hit when deps are missing
    yaml = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# The contact URL in the User-Agent must resolve, so it names the repo as it
# exists today. GitHub permanently redirects the old URL after a rename.
REPO_URL = "https://github.com/atalha1/islamweb-fatwa-notification"
VERSION = "1.1"

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("WATCHER_CONFIG", ROOT / "config.yaml"))
QUEUE_PATH = ROOT / "questions" / "queue.yaml"
STATE_PATH = ROOT / "state.json"
OBSERVATIONS_PATH = ROOT / "log" / "observations.csv"
REPORT_PATH = ROOT / "log" / "REPORT.md"

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
TELEGRAM_API = "https://api.telegram.org/bot%s/sendMessage"

STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"
STATE_UNKNOWN = "UNKNOWN"

HTTP_TIMEOUT = 25
POLL_INTERVAL_FLOOR = 20   # politeness floor; config cannot go below this


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Used when config.yaml is missing or a key is absent, so the tool always
# runs. config.yaml is the place to change things, not this dict.
DEFAULT_CONFIG = {
    "site": {
        "name": "Islamweb fatwa submission",
        "url": "https://www.islamweb.net/ar/fatwa/اسأل-عن-فتوى",
        "timezone_offset_hours": 3,
        "timezone_label": "Makkah",
    },
    "detection": {
        "closed_markers": ["نعتذر عن استقبال الأسئلة"],
        "closed_fallback_markers": ["نعتذر", "اكتمل العدد", "اكتمال العدد", "لا نستقبل"],
        "form_field_names": ["question", "guestname", "hidden_vercode", "btsubmit"],
        "form_fields_required": 2,
        "form_action_hints": ["fatwa", "ask", "question", "سؤال", "اسأل"],
        "require_textarea": True,
    },
    "schedule": {
        "active_hours": "0-23",
        "poll_interval_seconds": 20,
        "window_end_minute": 12,
        "consecutive_open_required": 2,
        "lookahead_from_minute": 40,
        "window_start_lead_seconds": 60,
    },
    "tracking": {
        "enabled": True,
        "url_template": "https://www.islamweb.net/ar/fatwa/{id}/",
        "not_published_markers": ["لا يوجد فتوي بهذا الرقم", "لا توجد فتوى بهذا الرقم"],
        "published_markers": ["تم نسخ الرابط", "السؤال"],
        "check_interval_hours": 6,
    },
    "alerts": {
        "body_preview_chars": 300,
        "unknown_alert_cooldown_hours": 24,
    },
}


def encode_url(url: str) -> str:
    """Percent-encode a URL's path without double-encoding an encoded one."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc,
                       quote(parts.path, safe="/-_.~%"), parts.query, parts.fragment))


def parse_hour_spec(spec) -> frozenset:
    """Parse "10-23" or "0-5,10,22-23" into a set of hours. Empty means all."""
    if spec is None or str(spec).strip() in ("", "*", "all"):
        return frozenset(range(24))
    hours = set()
    for chunk in str(spec).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            low, _, high = chunk.partition("-")
            try:
                low, high = int(low), int(high)
            except ValueError:
                continue
            if low <= high:
                hours.update(range(low, high + 1))
            else:  # a range that wraps midnight, e.g. "22-3"
                hours.update(range(low, 24))
                hours.update(range(0, high + 1))
        else:
            try:
                hours.add(int(chunk))
            except ValueError:
                continue
    return frozenset(h for h in hours if 0 <= h <= 23) or frozenset(range(24))


def load_config(path: Path = None) -> dict:
    """config.yaml layered over DEFAULT_CONFIG, one section at a time."""
    merged = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    path = path or CONFIG_PATH
    if yaml is not None and path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise SystemExit("config.yaml is not valid YAML: %s" % exc)
        for section, values in loaded.items():
            if isinstance(values, dict):
                merged.setdefault(section, {}).update(values)
    return merged


CONFIG = load_config()

SITE_NAME = CONFIG["site"]["name"]
PAGE_URL = encode_url(CONFIG["site"]["url"])
SITE_HOST = (urlsplit(PAGE_URL).hostname or "").lower()
# The registrable-ish suffix, so the never-submit guard also covers
# subdomains: www.islamweb.net -> islamweb.net.
SITE_DOMAIN = ".".join(SITE_HOST.split(".")[-2:]) if SITE_HOST.count(".") >= 1 else SITE_HOST
PAGE_PATH = urlsplit(PAGE_URL).path
ROBOTS_URL = urlunsplit((urlsplit(PAGE_URL).scheme, urlsplit(PAGE_URL).netloc,
                         "/robots.txt", "", ""))

TZ_OFFSET_HOURS = int(CONFIG["site"]["timezone_offset_hours"])
TZ_LABEL = CONFIG["site"]["timezone_label"]
SITE_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# The page renders one of these while submissions are closed.
CLOSED_MARKERS = tuple(CONFIG["detection"]["closed_markers"])
CLOSED_MARKER = CLOSED_MARKERS[0] if CLOSED_MARKERS else ""

# Broader phrases that also mean "closed". A deliberate safety net: a page
# that apologises or says the quota is full reads as CLOSED even if the
# primary marker was reworded. Being wrong in this direction costs a missed
# alert; being wrong the other way would fire a false alert every hour.
CLOSED_MARKER_FALLBACKS = tuple(CONFIG["detection"]["closed_fallback_markers"])

FORM_ACTION_HINTS = tuple(CONFIG["detection"]["form_action_hints"])
QUESTION_FIELD_NAMES = tuple(n.lower() for n in CONFIG["detection"]["form_field_names"])
QUESTION_FIELDS_REQUIRED = int(CONFIG["detection"]["form_fields_required"])
REQUIRE_TEXTAREA = bool(CONFIG["detection"]["require_textarea"])

ACTIVE_HOURS = parse_hour_spec(CONFIG["schedule"]["active_hours"])
POLL_INTERVAL_SECONDS = max(POLL_INTERVAL_FLOOR,
                            int(CONFIG["schedule"]["poll_interval_seconds"]))
WINDOW_END_MINUTE = int(CONFIG["schedule"]["window_end_minute"])
CONSECUTIVE_OPEN_REQUIRED = int(CONFIG["schedule"]["consecutive_open_required"])
LOOKAHEAD_FROM_MINUTE = int(CONFIG["schedule"]["lookahead_from_minute"])
WINDOW_START_LEAD_SECONDS = max(0, int(CONFIG["schedule"]["window_start_lead_seconds"]))

if not WINDOW_END_MINUTE < LOOKAHEAD_FROM_MINUTE < 60:
    raise SystemExit(
        "config error: schedule.lookahead_from_minute (%d) must be greater than "
        "schedule.window_end_minute (%d) and below 60. Below the window end, "
        "every run would roll forward to the next hour and never poll the one "
        "it was fired for."
        % (LOOKAHEAD_FROM_MINUTE, WINDOW_END_MINUTE))

# Worst case: a run that starts the moment the lookahead opens and polls right
# through to the window's end. Derived rather than hard-coded so that changing
# the schedule cannot silently leave the hard stop cutting a window short.
MAX_RUN_SECONDS = (60 - LOOKAHEAD_FROM_MINUTE + WINDOW_END_MINUTE + 2) * 60

TRACKING_ENABLED = bool(CONFIG["tracking"]["enabled"])
TRACK_URL_TEMPLATE = CONFIG["tracking"]["url_template"]
NOT_PUBLISHED_MARKERS = tuple(CONFIG["tracking"]["not_published_markers"])
PUBLISHED_MARKERS = tuple(CONFIG["tracking"]["published_markers"])
TRACK_INTERVAL_HOURS = int(CONFIG["tracking"]["check_interval_hours"])

BODY_PREVIEW_CHARS = int(CONFIG["alerts"]["body_preview_chars"])
UNKNOWN_ALERT_COOLDOWN_HOURS = int(CONFIG["alerts"]["unknown_alert_cooldown_hours"])

USER_AGENT = (
    "islamweb-watcher/%s (notification-only availability checker; "
    "read-only, never submits; +%s)" % (VERSION, REPO_URL)
)

OBSERVATIONS_HEADER = ["timestamp_utc", "timestamp_local", "http_status", "state"]


class NeverSubmitError(RuntimeError):
    """Raised if anything ever tries to write to the watched site."""


# --------------------------------------------------------------------------
# Logging (secret-safe)
# --------------------------------------------------------------------------

_SECRET_ENV_NAMES = (
    "CALLMEBOT_PHONE",
    "CALLMEBOT_APIKEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


def redact(text: str) -> str:
    """Strip any secret value out of a string before it reaches a log."""
    out = str(text)
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            out = out.replace(value, "<%s>" % name)
            out = out.replace(quote(value, safe=""), "<%s>" % name)
    # Belt and braces: scrub query params that carry credentials.
    out = re.sub(r"(apikey|phone|token|chat_id)=[^&\s]+", r"\1=<redacted>", out, flags=re.I)
    return out


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[%s] %s" % (stamp, redact(message)), flush=True)


# --------------------------------------------------------------------------
# Read-only HTTP session
# --------------------------------------------------------------------------


class ReadOnlyIslamwebSession(requests.Session):
    """A requests Session that physically cannot write to islamweb.net."""

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        host = (urlsplit(str(url)).hostname or "").lower()
        if host == SITE_DOMAIN or host.endswith("." + SITE_DOMAIN):
            if str(method).upper() != "GET":
                raise NeverSubmitError(
                    "Refusing %s to %s. This tool is notification-only and "
                    "must never submit the form." % (method, host)
                )
        return super().request(method, url, *args, **kwargs)


def make_session() -> ReadOnlyIslamwebSession:
    session = ReadOnlyIslamwebSession()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            # islamweb runs IIS with strict content negotiation: a narrow
            # Accept header earns an HTTP 406 instead of the page. Always
            # keep the */* fallback.
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "ar,en;q=0.8",
        }
    )
    return session


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def parse_robots_groups(robots_text: str) -> dict:
    """Parse robots.txt into {user_agent: {"rules": [(allow, path)], "crawl_delay": float|None}}."""
    groups: dict = {}
    current: list = []
    expecting_agent = True
    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not expecting_agent:
                current = []
                expecting_agent = True
            agent = value.lower()
            groups.setdefault(agent, {"rules": [], "crawl_delay": None})
            current.append(agent)
            continue
        expecting_agent = False
        if not current:
            continue
        for agent in current:
            if field == "disallow":
                groups[agent]["rules"].append((False, value))
            elif field == "allow":
                groups[agent]["rules"].append((True, value))
            elif field == "crawl-delay":
                try:
                    groups[agent]["crawl_delay"] = float(value)
                except ValueError:
                    pass
    return groups


def _rule_matches(pattern: str, path: str) -> bool:
    """RFC 9309 path matching, with * and $ wildcards."""
    if pattern == "":
        return False
    regex = ""
    for char in pattern:
        if char == "*":
            regex += ".*"
        elif char == "$":
            regex += "$"
        else:
            regex += re.escape(char)
    return re.match(regex, path) is not None


def robots_verdict(robots_text: str, path: str, agent: str = "*") -> dict:
    """Decide whether `agent` may fetch `path`. Longest match wins; Allow wins ties."""
    groups = parse_robots_groups(robots_text)
    group = groups.get(agent.lower()) or groups.get("*")
    if group is None:
        return {"allowed": True, "reason": "no matching user-agent group", "crawl_delay": None,
                "matched": None}
    best = None          # the winning (allow, pattern)
    best_key = None      # (pattern length, allow) - longest match wins, Allow breaks ties
    for allow, pattern in group["rules"]:
        if _rule_matches(pattern, path):
            key = (len(pattern), 1 if allow else 0)
            if best_key is None or key > best_key:
                best_key, best = key, (allow, pattern)
    if best is None:
        return {"allowed": True, "reason": "no rule matches the path",
                "crawl_delay": group["crawl_delay"], "matched": None}
    allow, pattern = best
    return {
        "allowed": bool(allow),
        "reason": "%s: %s" % ("Allow" if allow else "Disallow", pattern),
        "crawl_delay": group["crawl_delay"],
        "matched": pattern,
    }


def check_robots(session: requests.Session) -> dict:
    """Fetch robots.txt and decide whether polling is permitted."""
    try:
        response = session.get(
            ROBOTS_URL,
            timeout=HTTP_TIMEOUT,
            headers={"Accept": "text/plain,*/*;q=0.8"},
        )
    except requests.RequestException as exc:
        return {"allowed": None, "reason": "fetch failed: %s" % exc, "crawl_delay": None,
                "text": "", "status": 0}
    if response.status_code == 404:
        return {"allowed": True, "reason": "robots.txt returned 404 (nothing disallowed)",
                "crawl_delay": None, "text": "", "status": 404}
    if response.status_code != 200:
        return {"allowed": None, "reason": "robots.txt returned HTTP %d" % response.status_code,
                "crawl_delay": None, "text": response.text[:2000], "status": response.status_code}
    content_type = response.headers.get("Content-Type", "")
    if "html" in content_type.lower() or "<html" in response.text[:500].lower():
        return {"allowed": None,
                "reason": "robots.txt came back as HTML (%s), not a rules file"
                          % (content_type or "no content-type"),
                "crawl_delay": None, "text": response.text[:2000], "status": 200}
    robots_text = decode_response(response)
    verdict = robots_verdict(robots_text, PAGE_PATH)
    # Also report on the unencoded directory form, which is what a human reads.
    verdict["dir_verdict"] = robots_verdict(robots_text, "/ar/fatwa/")
    verdict["text"] = robots_text
    verdict["status"] = 200
    return verdict


# --------------------------------------------------------------------------
# Page classification
# --------------------------------------------------------------------------

def decode_response(response) -> str:
    """Decode a response the way a browser would.

    islamweb serves the fatwa page as `text/html` with **no charset**, so
    requests falls back to ISO-8859-1 and every Arabic byte comes back as
    mojibake - which silently breaks marker matching. Honour an explicit
    charset if there is one, then the document's own meta charset, then
    UTF-8. Never trust requests' guess.
    """
    content_type = response.headers.get("Content-Type", "")
    if "charset=" in content_type.lower():
        return response.text
    raw = response.content
    match = re.search(br"""charset\s*=\s*["']?\s*([A-Za-z0-9_-]+)""", raw[:4096], re.I)
    encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    for candidate in (encoding, "utf-8"):
        try:
            return raw.decode(candidate, errors="replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


_TAG_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_DIACRITICS_RE = re.compile(r"[ً-ْٰـ]")


def normalize_arabic(text: str) -> str:
    """Fold away diacritics, tatweel, alef variants and whitespace noise."""
    text = unicodedata.normalize("NFKC", text)
    text = _DIACRITICS_RE.sub("", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[آأإٱ]", "ا", text)  # alef variants
    text = text.replace("ى", "ي")  # alef maqsura -> ya
    text = text.replace("ة", "ه")  # ta marbuta -> ha
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(page_html: str) -> str:
    stripped = _TAG_RE.sub(" ", page_html)
    stripped = _ANY_TAG_RE.sub(" ", stripped)
    return html_mod.unescape(stripped)


def find_question_form(page_html: str, page_url: str = PAGE_URL):
    """Return the resolved action URL of a fatwa-question form, or None.

    A match requires all three of:
      * a <textarea> inside the form - the question body field,
      * a method that is POST or unspecified (the live form sets neither),
      * and EITHER at least two of the known question-form field names
        (question, guestname, hidden_vercode, btsubmit) OR an action
        resolving to a fatwa/question endpoint.

    The field-name signature is what keeps some other textarea-bearing form
    (a comment box, a feedback widget) from being mistaken for this one.
    """
    for match in re.finditer(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
                             page_html, re.I | re.S):
        attrs = match.group("attrs")
        body = match.group("body")
        if REQUIRE_TEXTAREA and not re.search(r"<textarea\b", body, re.I):
            continue
        method_match = re.search(r"method\s*=\s*['\"]?\s*(\w+)", attrs, re.I)
        method = (method_match.group(1) if method_match else "post").lower()
        if method != "post":
            continue
        action_match = re.search(r"action\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))", attrs, re.I)
        raw_action = ""
        if action_match:
            raw_action = next((g for g in action_match.groups() if g is not None), "")
        resolved = urljoin(page_url, raw_action.strip() or "")

        field_names = {
            name.lower() for name in
            re.findall(r"<(?:input|textarea|select)\b[^>]*?name\s*=\s*[\"']([^\"']+)",
                       body, re.I)
        }
        matched_fields = field_names.intersection(QUESTION_FIELD_NAMES)
        if len(matched_fields) >= QUESTION_FIELDS_REQUIRED:
            return resolved

        haystack = html_mod.unescape(resolved).lower()
        if any(hint.lower() in haystack for hint in FORM_ACTION_HINTS):
            return resolved
    return None


def classify(page_html: str, page_url: str = PAGE_URL):
    """Return (state, detail). state is OPEN, CLOSED or UNKNOWN."""
    if not page_html or not page_html.strip():
        return STATE_UNKNOWN, "empty response body"

    text = normalize_arabic(html_to_text(page_html))
    raw = normalize_arabic(page_html)
    for marker in CLOSED_MARKERS:
        folded = normalize_arabic(marker)
        if folded and (folded in text or folded in raw):
            return STATE_CLOSED, "closed marker present"
    for fallback in CLOSED_MARKER_FALLBACKS:
        folded = normalize_arabic(fallback)
        if folded in text or folded in raw:
            return STATE_CLOSED, "closed fallback marker present: %s" % fallback

    action = find_question_form(page_html, page_url)
    if action:
        return STATE_OPEN, "question form posts to %s" % action

    return STATE_UNKNOWN, "no closed marker and no question form found"


# --------------------------------------------------------------------------
# State file
# --------------------------------------------------------------------------

DEFAULT_STATE = {
    "paused": False,
    "paused_reason": None,
    "paused_at_utc": None,
    "telegram_update_offset": 0,
    "last_answer_check_utc": None,
    "last_unknown_alert_utc": None,
    "last_alert_utc": None,
    "last_alert_question_id": None,
    "pending_notification_retry": None,
}


def load_state() -> dict:
    state = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")) or {})
        except (json.JSONDecodeError, OSError) as exc:
            log("state.json unreadable (%s); starting from defaults" % exc)
    return state


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


# --------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------

QUEUE_FIELD_ORDER = ["id", "priority", "status", "lang", "title", "body",
                     "sent_at", "fatwa_ref", "fatwa_url", "answered_at"]


def load_queue(path: Path = QUEUE_PATH) -> list:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed. Run: pip install -r requirements.txt")
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError("%s must contain a YAML list of entries" % path)
    return data


def _leading_comment_block(path: Path) -> str:
    """The comment header at the top of the queue file.

    yaml.safe_dump cannot round-trip comments, so the header is captured and
    re-emitted verbatim. Without this, the first --mark-sent would silently
    delete the usage notes at the top of questions/queue.yaml.
    """
    if not path.exists():
        return ""
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            kept.append(line)
        else:
            break
    while kept and not kept[-1].strip():
        kept.pop()
    return ("\n".join(kept) + "\n\n") if kept else ""


def save_queue(entries: list, path: Path = QUEUE_PATH) -> None:
    header = _leading_comment_block(path)
    ordered = []
    for entry in entries:
        item = {key: entry.get(key) for key in QUEUE_FIELD_ORDER if key in entry}
        for key, value in entry.items():  # keep any extra keys the user added
            if key not in item:
                item[key] = value
        ordered.append(item)
    dumped = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False,
                            default_flow_style=False, width=100)
    path.write_text(header + dumped, encoding="utf-8")


def select_next_question(entries: list):
    """Lowest priority number first among queued entries; ties broken by id."""
    queued = [e for e in entries if str(e.get("status", "")).lower() == "queued"]
    if not queued:
        return None

    def sort_key(entry):
        try:
            priority = int(entry.get("priority", 10**9))
        except (TypeError, ValueError):
            priority = 10**9
        return (priority, str(entry.get("id", "")))

    return sorted(queued, key=sort_key)[0]


# --------------------------------------------------------------------------
# Pause / resume
# --------------------------------------------------------------------------

PAUSE_SUBMITTED = "submitted"   # set automatically by --mark-sent
PAUSE_MANUAL = "manual"         # set by --pause or a /pause message


def is_paused(state: dict) -> bool:
    return bool(state.get("paused"))


def set_paused(state: dict, paused: bool, reason: str = None) -> str:
    """Flip the pause flag and return a one-line human summary."""
    state["paused"] = bool(paused)
    state["paused_reason"] = reason if paused else None
    state["paused_at_utc"] = utc_now().isoformat() if paused else None
    if paused:
        return "Paused (%s). The submission page will not be polled." % (reason or "manual")
    return "Resumed. The submission page will be polled during %s." % describe_active_hours()


# --------------------------------------------------------------------------
# Answer tracking
# --------------------------------------------------------------------------

TRACK_PUBLISHED = "PUBLISHED"
TRACK_NOT_PUBLISHED = "NOT_PUBLISHED"
TRACK_UNKNOWN = "UNKNOWN"


def tracking_url(reference: str) -> str:
    """A pasted link is used as-is; a bare number goes through the template."""
    reference = str(reference).strip()
    if reference.lower().startswith(("http://", "https://")):
        return encode_url(reference)
    digits = re.sub(r"\D", "", reference)
    return encode_url(TRACK_URL_TEMPLATE.format(id=digits or reference))


def classify_answer_page(page_html: str):
    """Has this fatwa been published yet? Returns (state, detail).

    Ordered so that "not published" wins: Islamweb's not-found page still
    carries the site chrome, and mistaking it for an answer would fire a
    false "your question was answered" alert.
    """
    if not page_html or not page_html.strip():
        return TRACK_UNKNOWN, "empty response body"
    text = normalize_arabic(html_to_text(page_html))
    for marker in NOT_PUBLISHED_MARKERS:
        folded = normalize_arabic(marker)
        if folded and folded in text:
            return TRACK_NOT_PUBLISHED, "not-published marker present"
    for marker in PUBLISHED_MARKERS:
        folded = normalize_arabic(marker)
        if folded and folded in text:
            return TRACK_PUBLISHED, "published marker present: %s" % marker
    if not PUBLISHED_MARKERS:
        # With no positive markers configured, the absence of the
        # not-published phrase on a 200 response is the whole signal.
        return TRACK_PUBLISHED, "no not-published marker on a 200 response"
    return TRACK_UNKNOWN, "neither a published nor a not-published marker found"


def tracked_entries(entries: list) -> list:
    """Sent questions that carry a reference and are not answered yet."""
    out = []
    for entry in entries:
        if str(entry.get("status", "")).lower() != "sent":
            continue
        if entry.get("fatwa_ref") or entry.get("fatwa_url"):
            out.append(entry)
    return out


def compose_answered_message(entry, url: str, now: datetime = None) -> str:
    now = now or utc_now()
    return "\n".join([
        "📬 Your question has been answered",
        "%s time: %s" % (TZ_LABEL, local_now(now).strftime("%Y-%m-%d %H:%M")),
        "",
        "id: %s" % entry.get("id"),
        "title: %s" % entry.get("title"),
        "",
        url,
        "",
        "Marked as answered in the queue.",
    ])


def check_answers(state: dict, force: bool = False) -> int:
    """Poll every tracked question once. Returns how many were newly answered."""
    if not TRACKING_ENABLED:
        log("tracking is disabled in config.yaml")
        return 0

    entries = load_queue()
    tracked = tracked_entries(entries)
    if not tracked:
        log("nothing to track: no sent question carries a fatwa_ref or fatwa_url")
        return 0

    if not force:
        last = state.get("last_answer_check_utc")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                due = last_dt + timedelta(hours=TRACK_INTERVAL_HOURS)
                if utc_now() < due:
                    log("answer check not due until %s" % due.strftime("%Y-%m-%d %H:%MZ"))
                    return 0
            except ValueError:
                pass

    session = make_session()
    state["last_answer_check_utc"] = utc_now().isoformat()
    newly_answered = 0

    for entry in tracked:
        reference = entry.get("fatwa_url") or entry.get("fatwa_ref")
        url = tracking_url(reference)
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            log("%s: request failed (%s)" % (entry.get("id"), exc))
            continue
        if response.status_code != 200:
            log("%s: HTTP %d for %s" % (entry.get("id"), response.status_code, url))
            continue

        result, detail = classify_answer_page(decode_response(response))
        log("%s: %s (%s) %s" % (entry.get("id"), result, detail, url))

        if result != TRACK_PUBLISHED:
            continue

        entry["status"] = "answered"
        entry["answered_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["fatwa_url"] = url
        newly_answered += 1
        notify(compose_answered_message(entry, url), state)
        time.sleep(2)  # be gentle between lookups

    if newly_answered:
        save_queue(entries)
        log("%d question(s) answered" % newly_answered)
    return newly_answered


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def build_whatsapp_url(phone: str, text: str, apikey: str) -> str:
    """Build the fully percent-encoded CallMeBot URL (UTF-8, Arabic-safe)."""
    prepared = requests.Request(
        "GET", CALLMEBOT_URL, params={"phone": phone, "text": text, "apikey": apikey}
    ).prepare()
    return prepared.url


_WA_SUCCESS_HINTS = ("message queued", "message sent", "will be received", "sent to")
_WA_FAILURE_HINTS = ("error", "apikey is not", "apikey missing", "invalid", "not activated",
                     "couldn't", "could not", "failed")


def whatsapp_body_looks_ok(body: str) -> bool:
    lowered = (body or "").lower()
    if any(hint in lowered for hint in _WA_SUCCESS_HINTS):
        return True
    if any(hint in lowered for hint in _WA_FAILURE_HINTS):
        return False
    return bool(lowered.strip())


def send_whatsapp(text: str):
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        return False, "CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set"
    url = build_whatsapp_url(phone, text, apikey)
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
    except requests.RequestException as exc:
        return False, "whatsapp request failed: %s" % exc
    if response.status_code != 200:
        return False, "whatsapp HTTP %d" % response.status_code
    if not whatsapp_body_looks_ok(response.text):
        return False, "whatsapp body did not indicate success: %s" % response.text[:200]
    return True, "whatsapp ok"


def send_telegram(text: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    try:
        response = requests.post(
            TELEGRAM_API % token,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        return False, "telegram request failed: %s" % exc
    if response.status_code != 200:
        return False, "telegram HTTP %d: %s" % (response.status_code, response.text[:200])
    try:
        if not response.json().get("ok"):
            return False, "telegram returned ok=false"
    except ValueError:
        return False, "telegram returned non-JSON body"
    return True, "telegram ok"


def notify(text: str, state: dict, both: bool = False) -> bool:
    """WhatsApp first, Telegram as fallback. Returns True if any channel took it."""
    results = []
    wa_ok, wa_detail = send_whatsapp(text)
    results.append(wa_detail)
    log("whatsapp: %s" % wa_detail)

    tg_ok = False
    if both or not wa_ok:
        tg_ok, tg_detail = send_telegram(text)
        results.append(tg_detail)
        log("telegram: %s" % tg_detail)

    delivered = wa_ok or tg_ok
    if delivered:
        state["pending_notification_retry"] = None
    else:
        log("ALL CHANNELS FAILED: %s" % "; ".join(results))
        state["pending_notification_retry"] = {
            "text": text,
            "created_utc": utc_now().isoformat(),
        }
    return delivered


def retry_pending_notification(state: dict) -> None:
    """Retry a previously failed notification exactly once, then drop it."""
    pending = state.get("pending_notification_retry")
    if not pending or not pending.get("text"):
        return
    log("retrying notification queued at %s" % pending.get("created_utc"))
    state["pending_notification_retry"] = None  # one retry only, whatever happens
    save_state(state)
    wa_ok, wa_detail = send_whatsapp(pending["text"])
    log("retry whatsapp: %s" % wa_detail)
    if not wa_ok:
        tg_ok, tg_detail = send_telegram(pending["text"])
        log("retry telegram: %s" % tg_detail)


# --------------------------------------------------------------------------
# Telegram command interface
# --------------------------------------------------------------------------
#
# CallMeBot's WhatsApp bridge is send-only: there is no way to message it
# back. Telegram is therefore the control channel. Each run reads any new
# messages addressed to the bot and acts on them, so a /pause sent from a
# phone takes effect on the next run.

TELEGRAM_GETUPDATES = "https://api.telegram.org/bot%s/getUpdates"

COMMAND_HELP = """Commands:
/pause - stop checking the submission page
/resume - start checking it again
/status - what the watcher is doing right now
/sent <id> [ref] - mark a question submitted (pauses), optionally with its
    Islamweb question number or link so the answer can be tracked
/track <id> <ref> - attach a number or link to an already-sent question
/check - check tracked questions for answers right now
/next - show the question that would be sent next
/help - this message"""


def fetch_telegram_commands(state: dict):
    """New messages sent to the bot, oldest first. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return []
    offset = int(state.get("telegram_update_offset") or 0)
    try:
        response = requests.get(
            TELEGRAM_GETUPDATES % token,
            params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        log("could not read telegram commands: %s" % exc)
        return []
    if response.status_code != 200:
        log("telegram getUpdates HTTP %d" % response.status_code)
        return []
    try:
        payload = response.json()
    except ValueError:
        log("telegram getUpdates returned a non-JSON body")
        return []
    if not payload.get("ok"):
        log("telegram getUpdates returned ok=false")
        return []

    messages = []
    highest = offset
    for update in payload.get("result", []):
        highest = max(highest, int(update.get("update_id", 0)) + 1)
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        sender_chat = str((message.get("chat") or {}).get("id", ""))
        # Only obey the configured chat. Anyone else who finds the bot is
        # ignored - they must not be able to pause someone else's watcher.
        if not text or sender_chat != str(chat_id):
            continue
        messages.append(text)
    state["telegram_update_offset"] = highest
    return messages


def compose_status(state: dict) -> str:
    entries = load_queue()
    queued = [e for e in entries if str(e.get("status", "")).lower() == "queued"]
    tracked = tracked_entries(entries)
    lines = [
        "📋 %s watcher" % SITE_NAME,
        "",
        "Polling: %s" % ("PAUSED (%s)" % (state.get("paused_reason") or "manual")
                         if is_paused(state) else "active"),
        "Active hours: %s" % describe_active_hours(),
        "Queued questions: %d" % len(queued),
        "Awaiting an answer: %d" % len(tracked),
    ]
    nxt = select_next_question(entries)
    if nxt:
        lines.append("Next up: %s - %s" % (nxt.get("id"), nxt.get("title")))
    if is_paused(state):
        lines += ["", "Send /resume to start checking the submission page again."]
    return "\n".join(lines)


def handle_command(text: str, state: dict) -> str:
    """Run one command and return the reply to send back. Never raises."""
    parts = text.strip().split()
    if not parts:
        return ""
    command = parts[0].lower().lstrip("/")
    command = command.split("@", 1)[0]  # /pause@mybot
    args = parts[1:]

    if command in ("pause", "stop"):
        return set_paused(state, True, PAUSE_MANUAL)
    if command in ("resume", "start", "go"):
        return set_paused(state, False)
    if command == "status":
        return compose_status(state)
    if command in ("help", "commands"):
        return COMMAND_HELP
    if command == "next":
        nxt = select_next_question(load_queue())
        if not nxt:
            return "Nothing queued. Add a question to questions/queue.yaml."
        return compose_open_message(nxt)
    if command == "check":
        found = check_answers(state, force=True)
        return "Checked tracked questions. %d newly answered." % found
    if command in ("sent", "submitted"):
        if not args:
            return "Usage: /sent <id> [question number or link]"
        return mark_sent(args[0], args[1] if len(args) > 1 else None, state)[1]
    if command == "track":
        if len(args) < 2:
            return "Usage: /track <id> <question number or link>"
        return attach_reference(args[0], args[1])[1]
    return "Unknown command %r.\n\n%s" % (text.strip()[:40], COMMAND_HELP)


def process_commands(state: dict) -> int:
    """Read and run every pending command. Returns how many ran."""
    messages = fetch_telegram_commands(state)
    if not messages:
        return 0
    for text in messages:
        words = text.split()
        log("command received: %s" % (words[0] if words else "(empty)"))
        try:
            reply = handle_command(text, state)
        except Exception as exc:  # a bad command must never kill the run
            reply = "That command failed: %s" % exc
            log("command %r failed: %s" % (text[:40], exc))
        if reply:
            send_telegram(reply)
    save_state(state)
    return len(messages)


# --------------------------------------------------------------------------
# Message composition
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now(now: datetime = None) -> datetime:
    """The given moment (default: now) in the watched site's timezone."""
    return (now or utc_now()).astimezone(SITE_TZ)


def local_stamp(now: datetime = None) -> str:
    """ISO timestamp in the site's timezone, with its real offset."""
    moment = local_now(now)
    sign = "+" if TZ_OFFSET_HOURS >= 0 else "-"
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + "%s%02d:00" % (sign, abs(TZ_OFFSET_HOURS))


def compose_open_message(entry, now: datetime = None) -> str:
    now = now or utc_now()
    lines = [
        "🟢 Islamweb fatwa form is OPEN",
        "%s time: %s" % (TZ_LABEL, local_now(now).strftime("%Y-%m-%d %H:%M")),
        PAGE_URL,
        "",
    ]
    if entry is None:
        lines.append("No queued question. Add one to questions/queue.yaml.")
        return "\n".join(lines)

    body = str(entry.get("body") or "")
    preview = body[:BODY_PREVIEW_CHARS]
    if len(body) > BODY_PREVIEW_CHARS:
        preview += "…"
    lines += [
        "id: %s" % entry.get("id"),
        "title: %s" % entry.get("title"),
        "",
        preview,
        "",
        "After you submit, run:  python watch.py --mark-sent %s" % entry.get("id"),
    ]
    return "\n".join(lines)


def compose_unknown_message(detail: str, now: datetime = None) -> str:
    now = now or utc_now()
    return "\n".join([
        "⚠️ Islamweb watcher: page structure changed",
        "%s time: %s" % (TZ_LABEL, local_now(now).strftime("%Y-%m-%d %H:%M")),
        "The page matched neither the CLOSED marker nor a question form.",
        "Reason: %s" % detail,
        "A human needs to look at the markup and update the detection rules.",
        PAGE_URL,
    ])


# --------------------------------------------------------------------------
# Observations log
# --------------------------------------------------------------------------


def record_observation(http_status: int, state_name: str, now: datetime = None) -> None:
    now = now or utc_now()
    OBSERVATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not OBSERVATIONS_PATH.exists() or OBSERVATIONS_PATH.stat().st_size == 0
    with OBSERVATIONS_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if is_new:
            writer.writerow(OBSERVATIONS_HEADER)
        writer.writerow([
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            local_stamp(now),
            http_status,
            state_name,
        ])


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


def poll_once(session: requests.Session):
    """One GET. Returns (http_status, state, detail). Never raises."""
    try:
        response = session.get(PAGE_URL, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        return 0, STATE_UNKNOWN, "request failed: %s" % exc
    if response.status_code != 200:
        return response.status_code, STATE_UNKNOWN, "HTTP %d" % response.status_code
    state_name, detail = classify(decode_response(response), PAGE_URL)
    return 200, state_name, detail


def target_top_of_hour(now: datetime) -> datetime:
    """The opening this run is waiting for.

    Before LOOKAHEAD_FROM_MINUTE the run belongs to the hour it started in;
    from that minute on it is early for the next one. A run that starts in
    between - after this hour's window closed but before the lookahead opens -
    keeps the hour just gone, which leaves it a deadline in the past and so
    ends the run immediately. That is deliberate: a very late run would
    otherwise hold the runner until the next opening and, under the workflow's
    concurrency group, delay the run actually scheduled for it.
    """
    top_of_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= LOOKAHEAD_FROM_MINUTE:
        top_of_hour += timedelta(hours=1)
    return top_of_hour


def window_deadline(now: datetime) -> datetime:
    """End of this run's polling window: WINDOW_END_MINUTE past the target hour."""
    return target_top_of_hour(now) + timedelta(minutes=WINDOW_END_MINUTE)


def window_start(now: datetime) -> datetime:
    """First moment worth polling: shortly before the target hour opens."""
    return target_top_of_hour(now) - timedelta(seconds=WINDOW_START_LEAD_SECONDS)


def target_hour_local(now: datetime) -> int:
    """Which local hour's opening this run is waiting for."""
    return local_now(window_deadline(now)).hour


def is_active_hour(now: datetime) -> bool:
    """Is this run's target hour one we were told to watch?"""
    return target_hour_local(now) in ACTIVE_HOURS


def describe_active_hours() -> str:
    hours = sorted(ACTIVE_HOURS)
    if len(hours) == 24:
        return "every hour"
    runs, start = [], hours[0]
    for previous, current in zip(hours, hours[1:] + [None]):
        if current != previous + 1:
            runs.append((start, previous))
            start = current
    return ", ".join("%02d:00" % a if a == b else "%02d:00-%02d:00" % (a, b)
                     for a, b in runs) + " %s" % TZ_LABEL


def maybe_alert_unknown(state: dict, detail: str, now: datetime) -> None:
    """At most one 'structure changed' alert per 24h."""
    last = state.get("last_unknown_alert_utc")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if now - last_dt < timedelta(hours=UNKNOWN_ALERT_COOLDOWN_HOURS):
                log("UNKNOWN state, but the 24h alert cooldown is still active")
                return
        except ValueError:
            pass
    log("UNKNOWN state - alerting a human")
    notify(compose_unknown_message(detail, now), state)
    state["last_unknown_alert_utc"] = now.isoformat()


def run_watch(interval: int = POLL_INTERVAL_SECONDS) -> int:
    """The polling run. Returns a process exit code."""
    state = load_state()

    # --- commands first: a /resume sent from a phone must be able to wake a
    # paused watcher, so this runs even when everything below is skipped.
    process_commands(state)

    if is_paused(state):
        log("paused (%s) since %s - the submission page will not be polled. "
            "Send /resume to the Telegram bot to restart."
            % (state.get("paused_reason") or "manual",
               state.get("paused_at_utc") or "unknown"))
        check_answers(state)
        save_state(state)
        return 0

    # --- active-hours gate: before any request to the watched site. Outside
    # the configured hours this run must cost the site nothing, not even a
    # robots.txt fetch.
    started = utc_now()
    if not is_active_hour(started):
        log("target hour %02d:00 %s is outside the active window (%s) - nothing to do"
            % (target_hour_local(started), TZ_LABEL, describe_active_hours()))
        check_answers(state)
        save_state(state)
        return 0

    session = make_session()

    # --- robots.txt gate: re-checked on every run, not just once at design time.
    robots = check_robots(session)
    if robots["allowed"] is False:
        log("robots.txt DISALLOWS %s (%s). Polling is skipped." %
            (PAGE_PATH, robots["reason"]))
        log("Switch the workflow to degraded mode (see README).")
        save_state(state)
        return 0
    if robots["allowed"] is None:
        log("robots.txt could not be read (%s). Proceeding read-only and politely."
            % robots["reason"])
    else:
        log("robots.txt allows the page (%s)" % robots["reason"])

    crawl_delay = robots.get("crawl_delay") or 0
    interval = max(interval, int(crawl_delay) if crawl_delay else 0)
    if interval > POLL_INTERVAL_SECONDS:
        log("honouring robots.txt Crawl-delay: polling every %ds" % interval)

    retry_pending_notification(state)

    deadline = window_deadline(started)
    hard_stop = started + timedelta(seconds=MAX_RUN_SECONDS)
    log("run started %s | window closes %s | interval %ds"
        % (started.strftime("%H:%M:%SZ"), deadline.strftime("%H:%M:%SZ"), interval))

    # The cron fires well before the hour so that a late scheduler start is
    # still early. When it is not late, that lead time is not ours to spend on
    # the site: wait it out rather than polling a page that cannot have opened.
    opens_at = window_start(started)
    waiting = (opens_at - utc_now()).total_seconds()
    if waiting > 0:
        log("window opens %s - sleeping %dm%02ds before the first poll"
            % (opens_at.strftime("%H:%M:%SZ"), waiting // 60, waiting % 60))
        time.sleep(waiting)

    consecutive_open = 0
    polls = 0

    while True:
        now = utc_now()
        http_status, state_name, detail = poll_once(session)
        polls += 1
        record_observation(http_status, state_name, now)
        log("poll %d: status=%s state=%s (%s)" % (polls, http_status, state_name, detail))

        if state_name == STATE_OPEN:
            consecutive_open += 1
            if consecutive_open >= CONSECUTIVE_OPEN_REQUIRED:
                log("%d consecutive OPEN polls - alerting" % consecutive_open)
                entry = select_next_question(load_queue())
                message = compose_open_message(entry, now)
                if notify(message, state):
                    state["last_alert_utc"] = now.isoformat()
                    state["last_alert_question_id"] = (entry or {}).get("id")
                    save_state(state)
                    log("alert delivered - exiting run")
                    return 0
                save_state(state)
                log("alert delivery failed - will retry next run")
                return 0
        else:
            if consecutive_open:
                log("OPEN streak broken by %s - counter reset" % state_name)
            consecutive_open = 0
            if state_name == STATE_UNKNOWN and http_status == 200:
                # Only real markup changes trigger this; 5xx/timeouts do not.
                maybe_alert_unknown(state, detail, now)
                save_state(state)

        now = utc_now()
        if now >= deadline:
            log("window closed after %d polls" % polls)
            break
        if now >= hard_stop:
            log("hard runtime limit reached after %d polls" % polls)
            break
        time.sleep(min(interval, max(1, (deadline - now).total_seconds())))

    check_answers(state)
    save_state(state)
    return 0


# --------------------------------------------------------------------------
# Degraded mode (used only when robots.txt disallows polling)
# --------------------------------------------------------------------------


def run_reminder() -> int:
    """No scraping at all: just send the next queued question as a reminder."""
    state = load_state()
    retry_pending_notification(state)
    entry = select_next_question(load_queue())
    now = utc_now()
    if entry is None:
        log("nothing queued - no reminder sent")
        save_state(state)
        return 0
    body = str(entry.get("body") or "")
    preview = body[:BODY_PREVIEW_CHARS] + ("…" if len(body) > BODY_PREVIEW_CHARS else "")
    message = "\n".join([
        "⏰ Islamweb fatwa reminder (degraded mode - no scraping)",
        "%s time: %s" % (TZ_LABEL, local_now(now).strftime("%Y-%m-%d %H:%M")),
        "Submissions open at the top of the hour, Makkah time.",
        PAGE_URL,
        "",
        "id: %s" % entry.get("id"),
        "title: %s" % entry.get("title"),
        "",
        preview,
        "",
        "After you submit, run:  python watch.py --mark-sent %s" % entry.get("id"),
    ])
    notify(message, state)
    state["last_alert_utc"] = now.isoformat()
    state["last_alert_question_id"] = entry.get("id")
    save_state(state)
    return 0


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------


def cmd_once() -> int:
    session = make_session()
    http_status, state_name, detail = poll_once(session)
    now = utc_now()
    record_observation(http_status, state_name, now)
    print("utc     : %s" % now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    print("%-8s: %s" % (TZ_LABEL.lower()[:8], local_stamp(now)))
    print("http    : %s" % http_status)
    print("state   : %s" % state_name)
    print("detail  : %s" % detail)
    print("(--once never sends a notification)")
    return 0 if state_name != STATE_UNKNOWN else 1


def cmd_check_robots() -> int:
    session = make_session()
    verdict = check_robots(session)
    print("robots.txt : %s" % ROBOTS_URL)
    print("http       : %s" % verdict.get("status"))
    print("page path  : %s" % PAGE_PATH)
    print("allowed    : %s" % verdict["allowed"])
    print("reason     : %s" % verdict["reason"])
    print("crawl-delay: %s" % verdict.get("crawl_delay"))
    if verdict.get("dir_verdict"):
        print("/ar/fatwa/ : allowed=%s (%s)"
              % (verdict["dir_verdict"]["allowed"], verdict["dir_verdict"]["reason"]))
    text = verdict.get("text") or ""
    if text:
        print("\n----- robots.txt begin -----")
        print(text.strip())
        print("----- robots.txt end -----")
    if verdict["allowed"] is False:
        return 10
    if verdict["allowed"] is None:
        return 2
    return 0


def cmd_check_ref(reference: str) -> int:
    """Classify one reference without touching the queue or sending anything.

    Use it to confirm a question number resolves before trusting it:
        python watch.py --check-ref 447769
    """
    url = tracking_url(reference)
    print("reference : %s" % reference)
    print("url       : %s" % url)
    session = make_session()
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        print("result    : fetch failed (%s)" % exc)
        return 1
    print("http      : %d" % response.status_code)
    if response.status_code != 200:
        print("result    : not a 200, cannot classify")
        return 1
    result, detail = classify_answer_page(decode_response(response))
    print("result    : %s" % result)
    print("detail    : %s" % detail)
    print("(--check-ref never notifies and never edits the queue)")
    return 0 if result != TRACK_UNKNOWN else 2


def cmd_show_config() -> int:
    """Print what the tool actually resolved from config.yaml."""
    print("config file    : %s (%s)"
          % (CONFIG_PATH, "loaded" if CONFIG_PATH.exists() else "missing, using defaults"))
    print("site           : %s" % SITE_NAME)
    print("page url       : %s" % PAGE_URL)
    print("robots url     : %s" % ROBOTS_URL)
    print("guarded domain : %s (no non-GET request may reach it)" % SITE_DOMAIN)
    print("timezone       : %s (UTC%+d)" % (TZ_LABEL, TZ_OFFSET_HOURS))
    print("active hours   : %s" % describe_active_hours())
    print("poll interval  : %ds" % POLL_INTERVAL_SECONDS)
    print("window starts  : %ds before the hour" % WINDOW_START_LEAD_SECONDS)
    print("window ends    : :%02d past the hour" % WINDOW_END_MINUTE)
    print("looks ahead at : :%02d past the hour" % LOOKAHEAD_FROM_MINUTE)
    print("max run time   : %dm" % (MAX_RUN_SECONDS // 60))
    print("open needs     : %d consecutive OPEN polls" % CONSECUTIVE_OPEN_REQUIRED)
    print("closed markers : %s" % ", ".join(CLOSED_MARKERS))
    print("closed fallback: %s" % ", ".join(CLOSED_MARKER_FALLBACKS))
    print("form fields    : %s (need %d of them)"
          % (", ".join(QUESTION_FIELD_NAMES), QUESTION_FIELDS_REQUIRED))
    print("action hints   : %s" % ", ".join(FORM_ACTION_HINTS))
    print("queue          : %d entries" % len(load_queue()))
    return 0


def cmd_dump(url: str = None) -> int:
    """Print a page's structure so a human can re-tune detection. Read-only."""
    url = url or PAGE_URL
    session = make_session()
    print("url            : %s" % url)
    try:
        response = session.get(url, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        print("fetch failed: %s" % exc)
        return 1
    page = decode_response(response)
    print("http           : %s" % response.status_code)
    print("content-type   : %s" % response.headers.get("Content-Type"))
    print("bytes          : %d" % len(response.content))

    normalized = normalize_arabic(html_to_text(page))
    for label, needle in [
        ("CLOSED_MARKER", CLOSED_MARKER),
        ("نعتذر", "نعتذر"),
        ("استقبال", "استقبال"),
        ("اكتمال", "اكتمال"),
        ("العدد", "العدد"),
    ]:
        print("contains %-14s: %s" % (label, normalize_arabic(needle) in normalized))

    forms = list(re.finditer(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>",
                             page, re.I | re.S))
    print("\nforms found    : %d" % len(forms))
    for index, match in enumerate(forms, 1):
        attrs, body = match.group("attrs"), match.group("body")
        names = re.findall(r"<(?:input|textarea|select)\b[^>]*?name\s*=\s*[\"']([^\"']+)",
                           body, re.I)
        print("  [%d] attrs=%s" % (index, " ".join(attrs.split())[:180]))
        print("      textarea=%s fields=%s"
              % (bool(re.search(r"<textarea\b", body, re.I)), names[:12]))

    print("\n----- visible text, first 1200 chars -----")
    print(normalized[:1200])
    print("----- end -----")
    state_name, detail = classify(page, url)
    print("\nclassified as  : %s (%s)" % (state_name, detail))
    return 0


def read_observations(path: Path = None):
    """Every logged poll as a dict. Bad rows are skipped, never fatal."""
    path = path or OBSERVATIONS_PATH
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            stamp = (row.get("timestamp_local") or row.get("timestamp_makkah") or "").strip()
            state_name = (row.get("state") or "").strip().upper()
            if len(stamp) < 16 or state_name not in (STATE_OPEN, STATE_CLOSED, STATE_UNKNOWN):
                continue
            try:
                hour, minute = int(stamp[11:13]), int(stamp[14:16])
                second = int(stamp[17:19]) if len(stamp) >= 19 and stamp[16] == ":" else 0
            except ValueError:
                continue
            rows.append({"utc": (row.get("timestamp_utc") or "").strip(),
                         "local": stamp, "date": stamp[:10], "hour": hour,
                         "minute": minute, "second": second,
                         # Seconds past the top of the hour - polls are 20s
                         # apart, so minute resolution alone would collapse a
                         # three-poll window into "0 minutes".
                         "offset": minute * 60 + second,
                         "state": state_name,
                         "status": (row.get("http_status") or "").strip()})
    return rows


def build_report(rows) -> str:
    """Turn the observation log into the answer the log exists to give."""
    lines = ["# Observation report", ""]
    if not rows:
        lines += ["No observations logged yet. The report fills in as the",
                  "watcher runs - give it a few days."]
        return "\n".join(lines) + "\n"

    dates = sorted({row["date"] for row in rows})
    lines += [
        "Watching **%s** (%s)." % (SITE_NAME, PAGE_URL),
        "",
        "- Polls logged: **%d**" % len(rows),
        "- Days covered: **%d** (%s to %s)" % (len(dates), dates[0], dates[-1]),
        "- Active hours: %s" % describe_active_hours(),
        "",
        "## Open rate by hour (%s time)" % TZ_LABEL,
        "",
        "| Hour | Polls | OPEN | CLOSED | UNKNOWN | % open |",
        "|-----:|------:|-----:|-------:|--------:|-------:|",
    ]
    by_hour = {}
    for row in rows:
        bucket = by_hour.setdefault(row["hour"], {STATE_OPEN: 0, STATE_CLOSED: 0,
                                                  STATE_UNKNOWN: 0})
        bucket[row["state"]] += 1
    for hour in sorted(by_hour):
        counts = by_hour[hour]
        total = sum(counts.values())
        share = 100.0 * counts[STATE_OPEN] / total if total else 0.0
        lines.append("| %02d:00 | %d | %d | %d | %d | %.0f%% |"
                     % (hour, total, counts[STATE_OPEN], counts[STATE_CLOSED],
                        counts[STATE_UNKNOWN], share))

    # How long does a window stay open? Count consecutive OPEN polls per
    # (date, hour), which at a fixed interval is a duration.
    windows = {}
    for row in rows:
        if row["state"] == STATE_OPEN:
            key = (row["date"], row["hour"])
            windows.setdefault(key, []).append(row["offset"])
    lines += ["", "## Windows seen open", ""]
    if not windows:
        lines.append("No OPEN poll recorded yet.")
    else:
        lines += ["| Date | Hour | First seen | Last seen | Polls open | ~Duration |",
                  "|------|-----:|-----------:|----------:|-----------:|----------:|"]
        for (date, hour), offsets in sorted(windows.items()):
            first, last = min(offsets), max(offsets)
            # The window was open for at least the span between the first and
            # last OPEN poll, plus one interval - it was still open when we
            # last looked, and closed some time before the next poll.
            span = (last - first) + POLL_INTERVAL_SECONDS
            lines.append("| %s | %02d:00 | +%dm%02ds | +%dm%02ds | %d | ~%dm%02ds |"
                         % (date, hour, first // 60, first % 60, last // 60, last % 60,
                            len(offsets), span // 60, span % 60))
        open_hours = sorted({hour for _, hour in windows})
        lines += ["", "**Hours ever seen open:** %s"
                  % ", ".join("%02d:00" % h for h in open_hours)]
        never = sorted(set(by_hour) - set(open_hours))
        if never:
            lines.append("**Hours polled but never seen open:** %s"
                         % ", ".join("%02d:00" % h for h in never))

    unknown = [row for row in rows if row["state"] == STATE_UNKNOWN]
    if unknown:
        lines += ["", "## UNKNOWN readings", "",
                  "%d of %d polls could not be classified. A run of these means the "
                  "page markup changed - run `python watch.py --dump` and update "
                  "`config.yaml`." % (len(unknown), len(rows))]

    lines += ["", "---", "",
              "_Generated by `python watch.py --report`. "
              "Source data: `log/observations.csv`._"]
    return "\n".join(lines) + "\n"


def cmd_report(write: bool = False) -> int:
    report = build_report(read_observations())
    print(report)
    if write:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(report, encoding="utf-8")
        print("Written to %s" % REPORT_PATH.relative_to(ROOT))
    return 0


def cmd_test_alert() -> int:
    state = load_state()
    now = utc_now()
    message = "\n".join([
        "✅ Islamweb watcher test alert",
        "%s time: %s" % (TZ_LABEL, local_now(now).strftime("%Y-%m-%d %H:%M")),
        "If you can read this, alerts work. No page was scraped, "
        "nothing was submitted.",
    ])
    delivered = notify(message, state, both=True)
    save_state(state)
    return 0 if delivered else 1


def _git(*args) -> tuple:
    result = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def find_entry(entries: list, question_id: str):
    return next((e for e in entries if str(e.get("id")) == str(question_id)), None)


def mark_sent(question_id: str, reference: str = None, state: dict = None):
    """Mark a question submitted, record what to track, and pause polling.

    Pausing here is the point: once a question is in, there is nothing to
    watch the submission page for, so every further poll is wasted.
    """
    entries = load_queue()
    entry = find_entry(entries, question_id)
    if entry is None:
        return False, ("No queue entry with id %r. Known ids: %s"
                       % (question_id, ", ".join(str(e.get("id")) for e in entries)))
    entry["status"] = "sent"
    entry["sent_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if reference:
        entry["fatwa_ref"] = reference
    save_queue(entries)

    lines = ["Marked %s as sent." % question_id]
    if reference:
        lines.append("Tracking %s - you'll get a message when it is answered."
                     % tracking_url(reference))
    else:
        lines.append("No reference recorded, so the answer cannot be tracked. "
                     "Send /track %s <number or link> when you have it." % question_id)
    if state is not None:
        lines.append(set_paused(state, True, PAUSE_SUBMITTED))
        lines.append("Send /resume when you want to ask the next one.")
    return True, "\n".join(lines)


def attach_reference(question_id: str, reference: str):
    """Point an already-sent question at the number or link to watch."""
    entries = load_queue()
    entry = find_entry(entries, question_id)
    if entry is None:
        return False, "No queue entry with id %r." % question_id
    entry["fatwa_ref"] = reference
    if str(entry.get("status", "")).lower() == "queued":
        entry["status"] = "sent"
        entry["sent_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    save_queue(entries)
    return True, ("Tracking %s for %s. You'll get a message when it is answered."
                  % (tracking_url(reference), question_id))


def cmd_mark(question_id: str, new_status: str, fatwa_url: str = None) -> int:
    if new_status == "sent":
        state = load_state()
        ok, message = mark_sent(question_id, fatwa_url, state)
        print(message, file=sys.stdout if ok else sys.stderr)
        if not ok:
            return 1
        save_state(state)
    else:
        entries = load_queue()
        target = find_entry(entries, question_id)
        if target is None:
            print("No queue entry with id %r. Known ids: %s"
                  % (question_id, ", ".join(str(e.get("id")) for e in entries)),
                  file=sys.stderr)
            return 1
        target["status"] = new_status
        if new_status == "answered":
            target["answered_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        if fatwa_url:
            target["fatwa_url"] = fatwa_url
        save_queue(entries)
        print("Marked %s as %s." % (question_id, new_status))

    code, output = _git("add", str(QUEUE_PATH.relative_to(ROOT)),
                        str(STATE_PATH.relative_to(ROOT)))
    if code != 0:
        print("git add failed: %s" % output, file=sys.stderr)
        return 0
    code, output = _git("commit", "-m", "queue: mark %s as %s" % (question_id, new_status))
    print(output)
    if code == 0:
        print("Committed. Run 'git push' to publish.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Notification-only watcher for the Islamweb fatwa form. "
                    "This tool NEVER submits the form.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true",
                       help="single check, print the state, send nothing")
    group.add_argument("--run", action="store_true",
                       help="polling run until the window closes (used by CI)")
    group.add_argument("--test-alert", action="store_true",
                       help="send one test message through both channels")
    group.add_argument("--check-robots", action="store_true",
                       help="print the robots.txt verdict for the fatwa page")
    group.add_argument("--dump", action="store_true",
                       help="print the page structure for re-tuning detection")
    group.add_argument("--report", action="store_true",
                       help="summarise log/observations.csv: when does it open?")
    group.add_argument("--show-config", action="store_true",
                       help="print the effective configuration and exit")
    group.add_argument("--remind", action="store_true",
                       help="degraded mode: send the next queued question, no scraping")
    group.add_argument("--check-answers", action="store_true",
                       help="check tracked questions for published answers")
    group.add_argument("--commands", action="store_true",
                       help="read and run pending Telegram commands, then exit")
    group.add_argument("--pause", action="store_true",
                       help="stop polling the submission page")
    group.add_argument("--resume", action="store_true",
                       help="start polling the submission page again")
    group.add_argument("--status", action="store_true",
                       help="print what the watcher is currently doing")
    group.add_argument("--check-ref", metavar="REF",
                       help="classify one question number or link, send nothing")
    group.add_argument("--track", metavar="ID",
                       help="attach a question number or link to a sent entry "
                            "(use with --ref)")
    group.add_argument("--mark-sent", metavar="ID", help="mark a queue entry as sent")
    group.add_argument("--mark-answered", metavar="ID", help="mark a queue entry as answered")
    parser.add_argument("--fatwa-url", help="fatwa URL to record with --mark-answered")
    parser.add_argument("--ref", help="the Islamweb question number, or a full link, "
                                      "to track for an answer")
    parser.add_argument("--force", action="store_true",
                        help="with --check-answers, ignore the check interval")
    parser.add_argument("--url", help="with --dump, probe this URL instead of the "
                                     "configured page")
    parser.add_argument("--write", action="store_true",
                        help="with --report, also write log/REPORT.md")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS,
                        help="seconds between polls (minimum %d)" % POLL_INTERVAL_SECONDS)
    args = parser.parse_args(argv)

    if args.once:
        return cmd_once()
    if args.run:
        return run_watch(max(POLL_INTERVAL_SECONDS, args.interval))
    if args.test_alert:
        return cmd_test_alert()
    if args.check_robots:
        return cmd_check_robots()
    if args.dump:
        return cmd_dump(args.url)
    if args.report:
        return cmd_report(write=args.write)
    if args.show_config:
        return cmd_show_config()
    if args.remind:
        return run_reminder()
    if args.check_answers:
        state = load_state()
        found = check_answers(state, force=args.force)
        save_state(state)
        print("%d question(s) newly answered." % found)
        return 0
    if args.commands:
        state = load_state()
        ran = process_commands(state)
        print("%d command(s) processed." % ran)
        return 0
    if args.pause or args.resume:
        state = load_state()
        print(set_paused(state, bool(args.pause), PAUSE_MANUAL if args.pause else None))
        save_state(state)
        return 0
    if args.status:
        print(compose_status(load_state()))
        return 0
    if args.check_ref:
        return cmd_check_ref(args.check_ref)
    if args.track:
        if not args.ref:
            parser.error("--track needs --ref <question number or link>")
        ok, message = attach_reference(args.track, args.ref)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if args.mark_sent:
        return cmd_mark(args.mark_sent, "sent", args.ref or args.fatwa_url)
    if args.mark_answered:
        return cmd_mark(args.mark_answered, "answered", args.fatwa_url)
    parser.error("no command given")
    return 2


if __name__ == "__main__":
    sys.exit(main())
