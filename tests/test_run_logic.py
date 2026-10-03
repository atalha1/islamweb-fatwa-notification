"""Polling window, consecutive-OPEN debounce, alert channels, observations log."""
from datetime import datetime, timedelta, timezone

import watch


def utc(hour, minute):
    return datetime(2026, 9, 15, hour, minute, tzinfo=timezone.utc)


def test_a_run_fired_on_time_targets_the_next_hours_window():
    assert watch.window_deadline(utc(13, 40)) == utc(14, 15)


def test_a_delayed_run_still_targets_the_hour_it_was_scheduled_for():
    # Actions fired the :40 job late, at 14:03. The window is still 14:15.
    assert watch.window_deadline(utc(14, 3)) == utc(14, 15)


def test_a_run_that_starts_after_the_window_has_a_deadline_in_the_past():
    assert watch.window_deadline(utc(14, 20)) < utc(14, 20)


def test_the_cron_lead_absorbs_a_late_scheduler_without_losing_the_window():
    """The failure this schedule exists to prevent: a late run polling nothing.

    GitHub's scheduler is best-effort. Whatever it does to a job fired at
    :40 - on time, or anything up to the top of the hour late - the run must
    still come away with polling time inside the window.
    """
    for late_by in range(0, 21):           # :40 through :00, the design margin
        started = utc(13, 40) + timedelta(minutes=late_by)
        deadline = watch.window_deadline(started)
        assert deadline == utc(14, 15), "%s retargeted the wrong hour" % started
        assert deadline > started, "%s had no polling time left" % started


def test_polling_waits_for_the_hour_instead_of_burning_the_lead_time():
    """The lead time is scheduler slack, not licence to poll for 20 minutes."""
    opens = watch.window_start(utc(13, 40))
    assert opens == utc(14, 0) - timedelta(seconds=watch.WINDOW_START_LEAD_SECONDS)
    assert opens > utc(13, 40), "a run fired on time must sleep, not poll"
    assert opens < watch.window_deadline(utc(13, 40))


def test_a_run_fired_in_the_dead_zone_declines_rather_than_squatting():
    """Too late for this hour, too early to hold a runner until the next one."""
    started = utc(14, 25)                  # between window_end and lookahead
    assert watch.window_deadline(started) < started


def test_the_hard_stop_cannot_cut_a_window_short():
    longest = timedelta(minutes=60 - watch.LOOKAHEAD_FROM_MINUTE + watch.WINDOW_END_MINUTE)
    assert watch.MAX_RUN_SECONDS >= longest.total_seconds()


def test_site_time_is_utc_plus_three_all_year():
    for month in (1, 7):
        moment = datetime(2026, month, 1, 12, 0, tzinfo=timezone.utc)
        assert watch.local_now(moment).hour == 15
        assert watch.local_now(moment).utcoffset() == timedelta(hours=3)


def test_one_open_poll_is_not_enough_to_alert():
    assert watch.CONSECUTIVE_OPEN_REQUIRED == 2


def test_whatsapp_success_and_failure_bodies_are_told_apart():
    assert watch.whatsapp_body_looks_ok("Message queued. You will receive it shortly.")
    assert not watch.whatsapp_body_looks_ok("ERROR: APIKey is not valid")
    assert not watch.whatsapp_body_looks_ok("APIKey missing")
    assert not watch.whatsapp_body_looks_ok("")


def test_telegram_is_used_when_whatsapp_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "send_whatsapp", lambda t: (False, "wa down"))
    monkeypatch.setattr(watch, "send_telegram",
                        lambda t: (calls.append(t), (True, "tg ok"))[1])
    state = dict(watch.DEFAULT_STATE)
    assert watch.notify("hello", state) is True
    assert calls == ["hello"]
    assert state["pending_notification_retry"] is None


def test_telegram_is_skipped_when_whatsapp_succeeds(monkeypatch):
    monkeypatch.setattr(watch, "send_whatsapp", lambda t: (True, "wa ok"))
    monkeypatch.setattr(watch, "send_telegram",
                        lambda t: (_ for _ in ()).throw(AssertionError("should not run")))
    assert watch.notify("hello", dict(watch.DEFAULT_STATE)) is True


def test_both_channels_failing_sets_the_retry_flag(monkeypatch):
    monkeypatch.setattr(watch, "send_whatsapp", lambda t: (False, "wa down"))
    monkeypatch.setattr(watch, "send_telegram", lambda t: (False, "tg down"))
    state = dict(watch.DEFAULT_STATE)
    assert watch.notify("hello", state) is False
    assert state["pending_notification_retry"]["text"] == "hello"


def test_a_pending_retry_is_attempted_once_and_then_dropped(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    attempts = []
    monkeypatch.setattr(watch, "send_whatsapp",
                        lambda t: (attempts.append(t), (True, "ok"))[1])
    state = {"pending_notification_retry": {"text": "queued msg", "created_utc": "x"}}
    watch.retry_pending_notification(state)
    assert attempts == ["queued msg"]
    assert state["pending_notification_retry"] is None
    # A second pass must not resend.
    watch.retry_pending_notification(state)
    assert attempts == ["queued msg"]


def test_unknown_alert_is_rate_limited_to_one_per_24h(monkeypatch):
    sent = []
    monkeypatch.setattr(watch, "notify", lambda text, state, **kw: sent.append(text) or True)
    now = utc(13, 0)
    state = dict(watch.DEFAULT_STATE)
    watch.maybe_alert_unknown(state, "markup changed", now)
    assert len(sent) == 1
    watch.maybe_alert_unknown(state, "markup changed", now + timedelta(hours=5))
    assert len(sent) == 1
    watch.maybe_alert_unknown(state, "markup changed", now + timedelta(hours=25))
    assert len(sent) == 2


def test_observations_are_appended_with_a_header(monkeypatch, tmp_path):
    path = tmp_path / "log" / "observations.csv"
    monkeypatch.setattr(watch, "OBSERVATIONS_PATH", path)
    watch.record_observation(200, watch.STATE_CLOSED, utc(13, 52))
    watch.record_observation(200, watch.STATE_OPEN, utc(14, 0))
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "timestamp_utc,timestamp_local,http_status,state"
    assert lines[1] == "2026-09-15T13:52:00Z,2026-09-15T16:52:00+03:00,200,CLOSED"
    assert lines[2].endswith(",200,OPEN")


def test_the_shipped_observations_file_has_the_documented_header():
    header = watch.OBSERVATIONS_PATH.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(watch.OBSERVATIONS_HEADER)
