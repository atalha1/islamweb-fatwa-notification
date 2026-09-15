"""Pause/resume and the Telegram command interface."""
import pytest

import watch


@pytest.fixture
def state():
    return dict(watch.DEFAULT_STATE)


# --- pausing -------------------------------------------------------------

def test_a_fresh_state_is_not_paused(state):
    assert watch.is_paused(state) is False


def test_pausing_and_resuming_round_trips(state):
    watch.set_paused(state, True, watch.PAUSE_MANUAL)
    assert watch.is_paused(state) is True
    assert state["paused_reason"] == watch.PAUSE_MANUAL
    assert state["paused_at_utc"]
    watch.set_paused(state, False)
    assert watch.is_paused(state) is False
    assert state["paused_reason"] is None


def test_a_paused_run_makes_no_request_to_the_watched_site(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watch, "process_commands", lambda s: 0)
    monkeypatch.setattr(watch, "check_answers", lambda s, **k: 0)
    monkeypatch.setattr(watch, "load_state",
                        lambda: {"paused": True, "paused_reason": "submitted"})

    def explode():
        raise AssertionError("a paused watcher must not touch the site")

    monkeypatch.setattr(watch, "make_session", explode)
    assert watch.run_watch() == 0


def test_a_paused_run_still_reads_commands_so_resume_can_wake_it(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watch, "check_answers", lambda s, **k: 0)
    monkeypatch.setattr(watch, "load_state", lambda: {"paused": True})
    seen = []
    monkeypatch.setattr(watch, "process_commands", lambda s: seen.append(True) or 0)
    monkeypatch.setattr(watch, "make_session",
                        lambda: (_ for _ in ()).throw(AssertionError("no")))
    watch.run_watch()
    assert seen == [True], "commands must be read even while paused"


# --- commands ------------------------------------------------------------

def test_pause_and_resume_commands(state):
    assert "Paused" in watch.handle_command("/pause", state)
    assert watch.is_paused(state)
    assert "Resumed" in watch.handle_command("/resume", state)
    assert not watch.is_paused(state)


def test_commands_work_with_and_without_the_slash_and_bot_suffix(state):
    for text in ("/pause", "pause", "/pause@islamwebbot", "/PAUSE", "  /stop  "):
        watch.set_paused(state, False)
        assert "Paused" in watch.handle_command(text, state), text


def test_status_reports_what_is_happening(state):
    reply = watch.handle_command("/status", state)
    assert "Polling: active" in reply
    assert "10:00-23:00 Makkah" in reply
    watch.set_paused(state, True, watch.PAUSE_SUBMITTED)
    assert "PAUSED (submitted)" in watch.handle_command("/status", state)
    assert "/resume" in watch.handle_command("/status", state)


def test_help_lists_the_commands(state):
    reply = watch.handle_command("/help", state)
    for command in ("/pause", "/resume", "/status", "/sent", "/track", "/check"):
        assert command in reply


def test_an_unknown_command_explains_itself_instead_of_failing(state):
    reply = watch.handle_command("/frobnicate", state)
    assert "Unknown command" in reply and "/pause" in reply


def test_an_empty_message_is_ignored(state):
    assert watch.handle_command("   ", state) == ""


def test_sent_without_an_id_explains_the_usage(state):
    assert "Usage:" in watch.handle_command("/sent", state)


def test_track_without_a_reference_explains_the_usage(state):
    assert "Usage:" in watch.handle_command("/track q-001", state)


def test_sent_marks_the_question_pauses_and_records_the_reference(monkeypatch, state):
    entries = [{"id": "q-001", "priority": 1, "status": "queued", "title": "T"}]
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "save_queue", lambda *a, **k: None)

    reply = watch.handle_command("/sent q-001 447769", state)

    assert entries[0]["status"] == "sent"
    assert entries[0]["sent_at"]
    assert entries[0]["fatwa_ref"] == "447769"
    assert watch.is_paused(state), "submitting should stop the submission-page polling"
    assert state["paused_reason"] == watch.PAUSE_SUBMITTED
    assert "447769" in reply and "/resume" in reply


def test_sent_without_a_reference_says_tracking_is_not_possible_yet(monkeypatch, state):
    entries = [{"id": "q-001", "priority": 1, "status": "queued", "title": "T"}]
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "save_queue", lambda *a, **k: None)
    reply = watch.handle_command("/sent q-001", state)
    assert "cannot be tracked" in reply
    assert "/track q-001" in reply
    assert watch.is_paused(state)


def test_sent_with_an_unknown_id_is_reported_not_silently_ignored(monkeypatch, state):
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: [{"id": "q-001"}])
    monkeypatch.setattr(watch, "save_queue", lambda *a, **k: None)
    assert "No queue entry" in watch.handle_command("/sent nope", state)
    assert not watch.is_paused(state)


def test_track_attaches_a_reference_to_an_existing_entry(monkeypatch, state):
    entries = [{"id": "q-001", "status": "sent", "title": "T"}]
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "save_queue", lambda *a, **k: None)
    reply = watch.handle_command("/track q-001 447769", state)
    assert entries[0]["fatwa_ref"] == "447769"
    assert "447769" in reply


def test_a_command_that_raises_does_not_kill_the_run(monkeypatch, state, tmp_path):
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watch, "fetch_telegram_commands", lambda s: ["/check"])
    monkeypatch.setattr(watch, "check_answers",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    replies = []
    monkeypatch.setattr(watch, "send_telegram", lambda t: replies.append(t) or (True, "ok"))
    assert watch.process_commands(state) == 1
    assert "failed" in replies[0]


# --- who is allowed to command it ---------------------------------------

class FakeUpdates:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_only_the_configured_chat_can_control_the_watcher(monkeypatch, state):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    payload = {"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/pause"}},
        {"update_id": 2, "message": {"chat": {"id": 999}, "text": "/resume"}},
    ]}
    monkeypatch.setattr(watch.requests, "get", lambda *a, **k: FakeUpdates(payload))
    assert watch.fetch_telegram_commands(state) == ["/pause"]
    assert state["telegram_update_offset"] == 3


def test_the_update_offset_advances_so_commands_do_not_repeat(monkeypatch, state):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    payload = {"ok": True, "result": [
        {"update_id": 7, "message": {"chat": {"id": 111}, "text": "/status"}}]}
    monkeypatch.setattr(watch.requests, "get", lambda *a, **k: FakeUpdates(payload))
    watch.fetch_telegram_commands(state)
    assert state["telegram_update_offset"] == 8
    monkeypatch.setattr(watch.requests, "get",
                        lambda *a, **k: FakeUpdates({"ok": True, "result": []}))
    assert watch.fetch_telegram_commands(state) == []


def test_missing_telegram_credentials_disable_commands_quietly(monkeypatch, state):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert watch.fetch_telegram_commands(state) == []


def test_a_telegram_outage_does_not_break_the_run(monkeypatch, state):
    import requests
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setattr(watch.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("slow")))
    assert watch.fetch_telegram_commands(state) == []
