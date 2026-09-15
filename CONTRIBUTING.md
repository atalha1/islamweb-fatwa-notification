# Contributing

Small project, simple rules.

## The one rule that is not negotiable

**This tool never submits anything.** It is a notifier. A pull request that adds
auto-submission, form filling, captcha solving, or any non-`GET` request to the
watched site will be closed, however well written. `ReadOnlyIslamwebSession` and
`tests/test_safety.py` exist to enforce this; do not weaken either.

## Before opening a pull request

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

All tests must pass. If you change detection behaviour, add a test that fails
without your change — there are fixtures in `tests/fixtures/` to copy from.

## Good first contributions

- **A config for another site.** The most useful thing you can add. Drop it in
  `examples/` with a note on what the open and closed states look like.
- **Another alert channel** (ntfy, Discord, Signal, email). Follow the shape of
  `send_telegram`: return `(ok, detail)`, never raise, never log a secret.
- **Better reporting.** `build_report` is plain string building over
  `log/observations.csv`; there is a lot more that log could tell us.

## Things to keep in mind

- **Politeness is a feature.** The 20-second floor, the descriptive
  `User-Agent`, and the robots.txt gate are deliberate. Don't optimise them away.
- **Never log a secret.** Everything printed goes through `redact()`. If you add
  a credential, add its env var name to `_SECRET_ENV_NAMES`.
- **Fail soft.** A poll that errors should log and continue. The loop must never
  crash a run.
