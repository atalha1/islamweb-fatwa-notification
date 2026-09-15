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
from urllib.parse import quote, urljoin, urlsplit

import requests

try:
    import yaml
except ImportError:  # pragma: no cover - only hit when deps are missing
    yaml = None

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

REPO_URL = "https://github.com/atalha1/islamweb-watcher"
USER_AGENT = (
    "islamweb-watcher/1.0 (notification-only availability checker; "
    "read-only, never submits; +%s)" % REPO_URL
)

ISLAMWEB_HOST = "islamweb.net"
FATWA_PAGE_PATH = "/ar/fatwa/" + quote("اسأل-عن-فتوى", safe="-")
FATWA_PAGE_URL = "https://www.%s%s" % (ISLAMWEB_HOST, FATWA_PAGE_PATH)
ROBOTS_URL = "https://www.%s/robots.txt" % ISLAMWEB_HOST

# The page renders this apology block while submissions are closed.
CLOSED_MARKER = "نعتذر عن استقبال الأسئلة"

# Broader phrases that also mean "closed". The exact apology wording has not
# been observed live yet (every check so far caught the window open), so these
# are a deliberate safety net: a page that apologises or says the quota is full
# reads as CLOSED even if the primary marker was reworded. Being wrong in this
# direction costs a missed alert; being wrong the other way would fire a false
# alert every hour.
CLOSED_MARKER_FALLBACKS = (
    "نعتذر",           # "we apologise"
    "اكتمل العدد",     # "the quota is full"
    "اكتمال العدد",
    "لا نستقبل",       # "we are not accepting"
)

# A fatwa-question form posts to a path containing one of these hints.
FORM_ACTION_HINTS = ("fatwa", "ask", "question", "سؤال", "اسأل")

# The live form (observed 2026-09-15) carries no attributes at all - no
# method, no action - so the action heuristic alone is weak. Its field names
# are distinctive, though, and are the strongest signal the page gives us.
QUESTION_FIELD_NAMES = ("question", "guestname", "hidden_vercode", "btsubmit")
QUESTION_FIELDS_REQUIRED = 2

MAKKAH_TZ = timezone(timedelta(hours=3))  # UTC+3, no DST, ever.

POLL_INTERVAL_SECONDS = 20  # politeness floor; never lower this
CONSECUTIVE_OPEN_REQUIRED = 2
WINDOW_END_MINUTE = 12  # stop polling at :12 past the hour
HTTP_TIMEOUT = 25
MAX_RUN_SECONDS = 22 * 60  # hard stop, below the workflow timeout
UNKNOWN_ALERT_COOLDOWN_HOURS = 24
BODY_PREVIEW_CHARS = 300

ROOT = Path(__file__).resolve().parent
QUEUE_PATH = ROOT / "questions" / "queue.yaml"
STATE_PATH = ROOT / "state.json"
OBSERVATIONS_PATH = ROOT / "log" / "observations.csv"
OBSERVATIONS_HEADER = ["timestamp_utc", "timestamp_makkah", "http_status", "state"]

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
TELEGRAM_API = "https://api.telegram.org/bot%s/sendMessage"

STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"
STATE_UNKNOWN = "UNKNOWN"


class NeverSubmitError(RuntimeError):
    """Raised if anything ever tries to write to islamweb.net."""


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
        if host == ISLAMWEB_HOST or host.endswith("." + ISLAMWEB_HOST):
            if str(method).upper() != "GET":
                raise NeverSubmitError(
                    "Refusing %s to %s. This tool is notification-only and must "
                    "never submit the fatwa form." % (method, host)
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
    verdict = robots_verdict(robots_text, FATWA_PAGE_PATH)
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


def find_question_form(page_html: str, page_url: str = FATWA_PAGE_URL):
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
        if not re.search(r"<textarea\b", body, re.I):
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


def classify(page_html: str, page_url: str = FATWA_PAGE_URL):
    """Return (state, detail). state is OPEN, CLOSED or UNKNOWN."""
    if not page_html or not page_html.strip():
        return STATE_UNKNOWN, "empty response body"

    text = normalize_arabic(html_to_text(page_html))
    raw = normalize_arabic(page_html)
    marker = normalize_arabic(CLOSED_MARKER)
    if marker in text or marker in raw:
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

QUEUE_FIELD_ORDER = ["id", "priority", "status", "lang", "title", "body", "sent_at", "fatwa_url"]


def load_queue(path: Path = QUEUE_PATH) -> list:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed. Run: pip install -r requirements.txt")
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError("%s must contain a YAML list of entries" % path)
    return data


def save_queue(entries: list, path: Path = QUEUE_PATH) -> None:
    ordered = []
    for entry in entries:
        item = {key: entry.get(key) for key in QUEUE_FIELD_ORDER if key in entry}
        for key, value in entry.items():  # keep any extra keys the user added
            if key not in item:
                item[key] = value
        ordered.append(item)
    dumped = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False,
                            default_flow_style=False, width=100)
    path.write_text(dumped, encoding="utf-8")


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
# Message composition
# --------------------------------------------------------------------------


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def makkah_now(now: datetime = None) -> datetime:
    return (now or utc_now()).astimezone(MAKKAH_TZ)


def compose_open_message(entry, now: datetime = None) -> str:
    now = now or utc_now()
    lines = [
        "🟢 Islamweb fatwa form is OPEN",
        "Makkah time: %s" % makkah_now(now).strftime("%Y-%m-%d %H:%M"),
        FATWA_PAGE_URL,
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
        "Makkah time: %s" % makkah_now(now).strftime("%Y-%m-%d %H:%M"),
        "The page matched neither the CLOSED marker nor a question form.",
        "Reason: %s" % detail,
        "A human needs to look at the markup and update the detection rules.",
        FATWA_PAGE_URL,
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
            makkah_now(now).strftime("%Y-%m-%dT%H:%M:%S+03:00"),
            http_status,
            state_name,
        ])


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------


def poll_once(session: requests.Session):
    """One GET. Returns (http_status, state, detail). Never raises."""
    try:
        response = session.get(FATWA_PAGE_URL, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        return 0, STATE_UNKNOWN, "request failed: %s" % exc
    if response.status_code != 200:
        return response.status_code, STATE_UNKNOWN, "HTTP %d" % response.status_code
    state_name, detail = classify(decode_response(response), FATWA_PAGE_URL)
    return 200, state_name, detail


def window_deadline(now: datetime) -> datetime:
    """End of this run's polling window: WINDOW_END_MINUTE past the target hour."""
    top_of_hour = now.replace(minute=0, second=0, microsecond=0)
    if now.minute >= 40:  # started before the top of the next hour
        top_of_hour += timedelta(hours=1)
    return top_of_hour + timedelta(minutes=WINDOW_END_MINUTE)


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
    session = make_session()

    # --- robots.txt gate: re-checked on every run, not just once at design time.
    robots = check_robots(session)
    if robots["allowed"] is False:
        log("robots.txt DISALLOWS %s (%s). Polling is skipped." %
            (FATWA_PAGE_PATH, robots["reason"]))
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

    started = utc_now()
    deadline = window_deadline(started)
    hard_stop = started + timedelta(seconds=MAX_RUN_SECONDS)
    log("run started %s | window closes %s | interval %ds"
        % (started.strftime("%H:%M:%SZ"), deadline.strftime("%H:%M:%SZ"), interval))

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
        "Makkah time: %s" % makkah_now(now).strftime("%Y-%m-%d %H:%M"),
        "Submissions open at the top of the hour, Makkah time.",
        FATWA_PAGE_URL,
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
    print("makkah  : %s" % makkah_now(now).strftime("%Y-%m-%dT%H:%M:%S+03:00"))
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
    print("page path  : %s" % FATWA_PAGE_PATH)
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


def cmd_dump() -> int:
    """Print the page's structure so a human can re-tune detection. Read-only."""
    session = make_session()
    try:
        response = session.get(FATWA_PAGE_URL, timeout=HTTP_TIMEOUT)
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
    state_name, detail = classify(page, FATWA_PAGE_URL)
    print("\nclassified as  : %s (%s)" % (state_name, detail))
    return 0


def cmd_test_alert() -> int:
    state = load_state()
    now = utc_now()
    message = "\n".join([
        "✅ Islamweb watcher test alert",
        "Makkah time: %s" % makkah_now(now).strftime("%Y-%m-%d %H:%M"),
        "If you can read this, alerts work. No page was scraped, "
        "nothing was submitted.",
    ])
    delivered = notify(message, state, both=True)
    save_state(state)
    return 0 if delivered else 1


def _git(*args) -> tuple:
    result = subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def cmd_mark(question_id: str, new_status: str, fatwa_url: str = None) -> int:
    entries = load_queue()
    target = next((e for e in entries if str(e.get("id")) == str(question_id)), None)
    if target is None:
        print("No queue entry with id %r. Known ids: %s"
              % (question_id, ", ".join(str(e.get("id")) for e in entries)), file=sys.stderr)
        return 1
    target["status"] = new_status
    if new_status == "sent":
        target["sent_at"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    if fatwa_url:
        target["fatwa_url"] = fatwa_url
    save_queue(entries)
    print("Marked %s as %s." % (question_id, new_status))

    code, output = _git("add", str(QUEUE_PATH.relative_to(ROOT)))
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
    group.add_argument("--remind", action="store_true",
                       help="degraded mode: send the next queued question, no scraping")
    group.add_argument("--mark-sent", metavar="ID", help="mark a queue entry as sent")
    group.add_argument("--mark-answered", metavar="ID", help="mark a queue entry as answered")
    parser.add_argument("--fatwa-url", help="fatwa URL to record with --mark-answered")
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
        return cmd_dump()
    if args.remind:
        return run_reminder()
    if args.mark_sent:
        return cmd_mark(args.mark_sent, "sent", args.fatwa_url)
    if args.mark_answered:
        return cmd_mark(args.mark_answered, "answered", args.fatwa_url)
    parser.error("no command given")
    return 2


if __name__ == "__main__":
    sys.exit(main())
