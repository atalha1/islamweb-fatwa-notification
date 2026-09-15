# islamweb-watcher

Watches the Islamweb fatwa submission page and sends a WhatsApp message the
moment the form opens, with the next question from a local queue, so a human
can paste it and submit by hand.

---

## ⛔ The one rule

**This tool must never submit the form.**

It issues `GET` requests only, to read whether the submission window is open.
It never issues a `POST`, `PUT`, `PATCH` or `DELETE` to `islamweb.net`, never
fills in a form field, never replays a form, and never automates a submission
in any way. Its entire output is a notification to a human.

This is not just a promise in a comment. It is enforced at runtime by
`ReadOnlyIslamwebSession` in `watch.py`, which raises `NeverSubmitError` on any
non-`GET` request to `islamweb.net`, and by a test
(`tests/test_safety.py::test_a_post_to_islamweb_raises_instead_of_being_sent`)
that fails the build if the guard is removed.

Do not remove the guard. Do not add an auto-submit flag.

---

## robots.txt finding

<!-- ROBOTS-FINDING-START -->
**Checked 15 September 2026 from a GitHub Actions runner. `/ar/fatwa/` is NOT
disallowed, so the polling design stands.**

`https://www.islamweb.net/robots.txt` is two lines, in full:

```
User-agent: *
Disallow: /newislamweb/
```

There is one group, for the generic `*` user-agent, and it disallows exactly
one path prefix: `/newislamweb/`. There is no rule matching `/ar/fatwa/`, no
`Crawl-delay`, and no `Sitemap`. Polling the fatwa page with a generic user
agent is permitted.

One practical gotcha found while checking, worth knowing if you ever curl this
site by hand: islamweb runs IIS with strict content negotiation. Requesting
`robots.txt` with `Accept: text/html` gets you an **HTTP 406** error page
rather than the file. The watcher now sends `Accept: text/plain,*/*` for
robots and keeps a `*/*` fallback on the page request.
<!-- ROBOTS-FINDING-END -->

The watcher does not take this finding on trust. **Every run re-fetches
`https://www.islamweb.net/robots.txt` before polling** and:

* if the fatwa page is **disallowed** for the generic `*` user-agent, it logs
  the matching rule, **skips polling entirely**, and exits cleanly;
* if `robots.txt` declares a `Crawl-delay` longer than 20s, the poll interval
  is raised to match it;
* if `robots.txt` cannot be read, it proceeds read-only at the 20s floor.

So if Islamweb changes its `robots.txt` tomorrow, the tool stops polling on its
own without anyone touching it.

### Verifying robots.txt yourself

```bash
python watch.py --check-robots          # prints the verdict and the full file
```

Exit codes: `0` allowed, `10` disallowed, `2` unreadable. You can also run it on
a GitHub runner: **Actions → watch → Run workflow → mode: `check-robots`**.

---

## How detection works

| State | Condition |
|---|---|
| `CLOSED` | the page contains `نعتذر عن استقبال الأسئلة`, **or** one of the fallback phrases `نعتذر`, `اكتمل العدد`, `اكتمال العدد`, `لا نستقبل` |
| `OPEN` | that string is **absent** *and* the page contains a `<form method="post">` whose action resolves to a fatwa/question endpoint *and* which contains a `<textarea>` |
| `UNKNOWN` | neither condition holds — the markup changed |

"Looks like a fatwa-question endpoint" means **either** the form carries at
least two of the field names the live form uses — `question`, `guestname`,
`hidden_vercode`, `btsubmit` — **or** its action resolves to a path containing
a fatwa/ask/question hint. The field-name signature is the stronger of the two
and the reason the first is tried first (see "What the live page actually looks
like" below).

Arabic matching is done after folding away diacritics, tatweel,
alef/ya/ta-marbuta variants and whitespace, so the marker still matches if it
is split across tags or restyled.

### What the live page actually looks like

Two things were found by dumping the real page from a runner
(`python watch.py --dump`), and both would have broken a textbook
implementation:

**The page is served as `text/html` with no `charset`.** `requests` then falls
back to ISO-8859-1, and `response.text` returns mojibake for every Arabic
character — so a naive `if MARKER in response.text` is always `False` and the
watcher reports `OPEN` forever. `decode_response()` honours an explicit
charset, then the document's own `<meta charset>`, then UTF-8, and never
trusts the guess.

**The question form has no attributes at all** — literally `<form>`, with no
`method` and no `action`. It is the only `<form>` on the page (the site search
is JavaScript, not a form), and its fields are `visitor_id`, `guestname`,
`email`, `gender`, `hidden_vercode`, `question`, `remLen`, `btcheck`,
`btsubmit`. Matching on the action alone was therefore weak, which is why the
field-name signature exists.

If either of these changes, `--dump` is the tool for re-tuning: it prints every
form with its fields, the marker checks, and the decoded visible text, and it
sends nothing.

The fallback phrases are a deliberate safety net. Every live check so far has
caught the window **open**, so the exact apology wording has not been seen with
its Arabic decoded correctly. Erring towards `CLOSED` costs at most a missed
alert; erring the other way would fire a false alert every single hour. Once a
closed page has been observed, `--dump` will show the real wording and the
fallbacks can be tightened.

**Debounce:** two consecutive `OPEN` polls, 20 seconds apart, are required
before an alert fires. A single garbage response cannot trigger one.

**On `UNKNOWN`:** at most one "the page structure changed, a human should look"
alert per 24 hours, tracked in `state.json`. Network errors and non-200
responses are logged as `UNKNOWN` in the observations log but deliberately do
**not** trigger that alert — only a genuine HTTP 200 that fails to classify
does. Nothing in the poll loop can crash the run.

---

## Schedule

`.github/workflows/watch.yml` runs on cron `52 * * * *` (UTC). Each run polls
every 20 seconds until 12 minutes past the hour, then exits. It exits
immediately after the first successful alert.

Makkah is UTC+3 with no DST, so the top of the hour is the same instant in both
zones — no conversion is needed for the schedule itself.

> **Cost.** A run polls for up to 20 minutes, so this is ~480 Actions minutes a
> day. That is free on a public repository and blows through the free quota in
> about four days on a private one. See Step 0 of the setup.

> **Known limitation.** GitHub's scheduler is best-effort and can fire several
> minutes late when the platform is busy. `watch.py` computes its window from
> the real wall clock rather than from its start time, so a late start still
> polls up to `:12`; but if a run starts after `:12` it records one observation
> and exits. If you find you are missing windows, change the cron to
> `45 * * * *` — the script just polls a little longer. Actions minutes are
> free on public repositories.

**Politeness:** 20s minimum interval, hard floor in code; a descriptive
`User-Agent` naming this repository so an operator can find a human; no retry
storms — a failed fetch waits for the next scheduled poll like any other.

---

## Queue

`questions/queue.yaml` is a list of entries:

```yaml
- id: q-001
  priority: 1            # int, lower goes first
  status: queued         # queued | sent | answered
  lang: ar
  title: ...
  body: ...
  sent_at: null
  fatwa_url: null
```

On alert, the lowest-priority `queued` entry is chosen (ties broken by `id`, so
selection is deterministic). The message carries its `id`, `title`, and the
first 300 characters of `body`.

**Nothing is marked sent automatically.** After you submit by hand:

```bash
python watch.py --mark-sent q-001
git push                                   # the mark-sent step commits for you

# later, when the answer is published:
python watch.py --mark-answered q-001 --fatwa-url https://www.islamweb.net/...
```

---

## Alerts

**Primary — WhatsApp via CallMeBot:** `GET https://api.callmebot.com/whatsapp.php`
with `phone`, `text` (percent-encoded UTF-8) and `apikey`.

**Fallback — Telegram:** `sendMessage`. Fires if the WhatsApp call returns
non-200 or a body that does not indicate success.

If **both** fail, the failure is written to the run log and the message is
stored in `state.json` under `pending_notification_retry`. The next run retries
it exactly once, then drops it.

Arabic is percent-encoded by `requests`' own encoder;
`tests/test_encoding.py` proves an Arabic string round-trips through the query
string unchanged, including newlines, `&` and `=`.

### Secrets

Four GitHub Actions repository secrets. Never hardcoded, never committed, and
scrubbed from every log line by `redact()` before printing:

| Secret | Used for |
|---|---|
| `CALLMEBOT_PHONE` | WhatsApp destination, international format, e.g. `+9715XXXXXXX` |
| `CALLMEBOT_APIKEY` | CallMeBot API key |
| `TELEGRAM_BOT_TOKEN` | Telegram fallback |
| `TELEGRAM_CHAT_ID` | Telegram fallback |

`.env.example` holds the same four names with empty values for local testing.
`.env` is gitignored.

---

## The observations log

Every poll appends a row to `log/observations.csv`:

```
timestamp_utc,timestamp_makkah,http_status,state
```

The workflow commits this file after each run. After two weeks it will answer:
is the opening genuinely hourly, how long does the window stay open, and which
hours fill instantly. That dataset decides which hour to target.

---

## Local use

```bash
pip install -r requirements-dev.txt

python watch.py --once           # one check, prints the state, sends NOTHING
python watch.py --dump           # page structure: forms, fields, decoded text
python watch.py --check-robots   # robots.txt verdict + the full file
python watch.py --test-alert     # one test message through BOTH channels
python watch.py --run            # a full polling run (what CI does)
python watch.py --remind         # degraded mode: send the next question, no scraping

python -m pytest tests -q
```

---

## Degraded mode

If `robots.txt` ever disallows the page, polling stops by itself. The fallback
is `.github/workflows/daily-reminder.yml`: no scraping, just one WhatsApp
reminder a day containing the next queued question. It uses the same queue and
the same alert code.

To switch over: disable the **watch** workflow in the Actions tab, then
uncomment the `schedule:` block in `daily-reminder.yml` and set the hour.

---

## Manual setup

### Step 0 — make the repository public (do this one first)

This repository is currently **private**, and that is not cosmetic. On a private
repository GitHub Actions is metered: the free tier gives 2,000 minutes a month.
This watcher runs up to 20 minutes every hour, which is roughly **480 minutes a
day** — the monthly quota is gone in about four days and the watcher silently
stops running. **On a public repository Actions minutes are unlimited**, which is
what this design assumes.

Repository → **Settings → General → Danger Zone → Change visibility → Make
public**.

While you are on that page, **Settings → General → Repository name**: rename it
to `islamweb-watcher` if you want the name you asked for. GitHub permanently
redirects the old URL, so nothing breaks — not your local clone's remote, and not
the contact URL in the watcher's User-Agent.

Nothing in this repository contains a secret: the four credentials live in
Actions secrets, never in the tree, and `.env` is gitignored. The observations
log and the question queue are the only data here, and both are meant to be
shared.

### Step 1 — get a CallMeBot API key 

Takes about a minute. Save `+34 644 51 95 23` to your phone's contacts as *CallMeBot*, then send it
this exact WhatsApp message:

```
I allow callmebot to send me messages
```

It replies with your API key. The key is tied to the number you messaged from.

### Step 2 — create a Telegram bot for the fallback (optional but recommended)

Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow the prompts →
copy the token. Then send your new bot any message, open
`https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser, and copy
`result[0].message.chat.id`.

### Step 3 — add the four secrets

Repository → **Settings → Secrets and variables → Actions → New repository
secret**. Add `CALLMEBOT_PHONE`, `CALLMEBOT_APIKEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`.

### Step 4 — let the workflow commit the log

Repository → **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions** → Save. Without this the watcher still alerts,
but cannot commit `log/observations.csv`.

Then verify: **Actions → watch → Run workflow → mode: `test-alert`**. You should
get a WhatsApp message and a Telegram message.

Finally, replace the three placeholder entries in `questions/queue.yaml` with
your real questions.

---

## Verified live

Everything below was run against the real site from a GitHub Actions runner on
15 September 2026, not just against fixtures:

* `robots.txt` fetched and parsed — `/ar/fatwa/` is not disallowed.
* The fatwa page fetched, decoded and classified — `OPEN` at 13:02 Makkah,
  with the question form and its submission instructions present, which is
  exactly what the top of the hour should look like.
* The `--dump` output confirmed the form's field names and that the page
  carries no charset header.

Not yet verified live: the page in its **closed** state. The first scheduled
run that catches it will record a `CLOSED` row in `log/observations.csv`; if it
records `UNKNOWN` instead, you will get the one-per-24h "structure changed"
alert and `--dump` will show the real apology wording.

## Stack

Python 3.11, `requests` for HTTP. `PyYAML` is the one additional dependency —
the queue is a YAML file and needs a parser; it is pure Python with no
transitive dependencies. No framework, no database. `pytest` for tests, dev
only.

## Files

```
watch.py                            the whole tool
questions/queue.yaml                your question queue
log/observations.csv                one row per poll, committed by CI
state.json                          alert cooldowns and the retry flag (created on first run)
.github/workflows/watch.yml         the hourly poller
.github/workflows/tests.yml         CI
.github/workflows/daily-reminder.yml  degraded mode, disabled by default
tests/                              67 tests
```
