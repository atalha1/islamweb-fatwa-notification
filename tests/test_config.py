"""Configuration loading, hour specs, and the active-hours gate."""
from datetime import datetime, timezone

import pytest

import watch


def utc(hour, minute):
    return datetime(2026, 9, 15, hour, minute, tzinfo=timezone.utc)


# --- hour specs ----------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("10-23", set(range(10, 24))),
    ("0-23", set(range(24))),
    ("9", {9}),
    ("0-5,10,22-23", {0, 1, 2, 3, 4, 5, 10, 22, 23}),
    (" 10 - 12 ", {10, 11, 12}),
])
def test_hour_specs_parse(spec, expected):
    assert set(watch.parse_hour_spec(spec)) == expected


def test_a_range_that_wraps_midnight_is_understood():
    assert set(watch.parse_hour_spec("22-3")) == {22, 23, 0, 1, 2, 3}


@pytest.mark.parametrize("spec", [None, "", "*", "all", "nonsense", "99-200"])
def test_an_empty_or_unusable_spec_means_every_hour(spec):
    assert set(watch.parse_hour_spec(spec)) == set(range(24))


def test_out_of_range_hours_are_dropped_not_kept():
    assert set(watch.parse_hour_spec("22-25")) == {22, 23}


# --- the overnight gate --------------------------------------------------

def test_the_shipped_config_sleeps_overnight_and_wakes_at_ten():
    """The user asked for no requests between midnight and 10:00 Makkah."""
    assert set(watch.ACTIVE_HOURS) == set(range(10, 24))
    for hour in range(0, 10):
        assert hour not in watch.ACTIVE_HOURS, "%02d:00 should be asleep" % hour


def test_a_run_targeting_an_active_hour_proceeds():
    # 06:52 UTC targets 07:00 UTC = 10:00 Makkah, the first active hour.
    assert watch.target_hour_local(utc(6, 52)) == 10
    assert watch.is_active_hour(utc(6, 52)) is True


def test_a_run_targeting_the_quiet_hours_is_skipped():
    # 20:52 UTC targets 21:00 UTC = 00:00 Makkah - the first quiet hour.
    assert watch.target_hour_local(utc(20, 52)) == 0
    assert watch.is_active_hour(utc(20, 52)) is False
    # 02:52 UTC targets 03:00 UTC = 06:00 Makkah, still quiet.
    assert watch.is_active_hour(utc(2, 52)) is False


def test_the_last_active_run_targets_2300_makkah():
    assert watch.target_hour_local(utc(19, 52)) == 23
    assert watch.is_active_hour(utc(19, 52)) is True


def test_the_gate_uses_the_target_hour_not_the_start_hour():
    """A run starting at 09:52 Makkah is watching for the 10:00 opening."""
    assert watch.target_hour_local(utc(6, 52)) == 10  # 09:52 Makkah start
    assert watch.is_active_hour(utc(6, 52)) is True


def test_run_watch_exits_without_polling_outside_active_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(watch, "is_active_hour", lambda now: False)

    def explode(*args, **kwargs):
        raise AssertionError("no network call may happen outside active hours")

    monkeypatch.setattr(watch, "make_session", explode)
    assert watch.run_watch() == 0


def test_active_hours_are_described_readably():
    assert watch.describe_active_hours() == "10:00-23:00 Makkah"
    monkey = watch.ACTIVE_HOURS
    try:
        watch.ACTIVE_HOURS = watch.parse_hour_spec("0-23")
        assert watch.describe_active_hours() == "every hour"
        watch.ACTIVE_HOURS = watch.parse_hour_spec("9,14-16")
        assert watch.describe_active_hours() == "09:00, 14:00-16:00 Makkah"
    finally:
        watch.ACTIVE_HOURS = monkey


# --- config loading ------------------------------------------------------

def test_config_yaml_layers_over_the_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("site:\n  name: Something else\n", encoding="utf-8")
    config = watch.load_config(path)
    assert config["site"]["name"] == "Something else"
    # Untouched keys keep their defaults.
    assert config["site"]["timezone_offset_hours"] == 3
    assert config["schedule"]["window_end_minute"] == 12


def test_a_missing_config_file_still_yields_a_working_config(tmp_path):
    config = watch.load_config(tmp_path / "does-not-exist.yaml")
    assert config["site"]["url"].startswith("https://")
    assert config["detection"]["closed_markers"]


def test_invalid_yaml_fails_loudly_rather_than_silently(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("site:\n  name: [unclosed\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        watch.load_config(path)


def test_the_poll_interval_floor_cannot_be_configured_away(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("schedule:\n  poll_interval_seconds: 1\n", encoding="utf-8")
    config = watch.load_config(path)
    assert max(watch.POLL_INTERVAL_FLOOR,
               config["schedule"]["poll_interval_seconds"]) == watch.POLL_INTERVAL_FLOOR


def test_the_shipped_config_file_matches_what_the_module_resolved():
    config = watch.load_config(watch.CONFIG_PATH)
    assert config["site"]["timezone_label"] == watch.TZ_LABEL
    assert config["schedule"]["active_hours"] == "10-23"


# --- url handling --------------------------------------------------------

def test_arabic_urls_are_percent_encoded_once_not_twice():
    once = watch.encode_url("https://example.com/ar/اسأل")
    assert once.encode("ascii")
    assert watch.encode_url(once) == once, "re-encoding must be a no-op"


def test_the_guarded_domain_is_derived_from_the_configured_url():
    assert watch.SITE_DOMAIN == "islamweb.net"
    assert watch.ROBOTS_URL == "https://www.islamweb.net/robots.txt"


# --- shipped examples ----------------------------------------------------

def test_every_example_config_is_valid_and_complete():
    """The examples are documentation; broken ones mislead people."""
    examples = sorted((watch.ROOT / "examples").glob("*.yaml"))
    assert examples, "expected example configs to ship"
    for path in examples:
        config = watch.load_config(path)
        for section in ("site", "detection", "schedule", "alerts"):
            assert section in config, "%s is missing [%s]" % (path.name, section)
        assert config["site"]["url"].startswith("http"), path.name
        assert config["detection"]["closed_markers"], path.name
        assert watch.parse_hour_spec(config["schedule"]["active_hours"]), path.name
        assert watch.encode_url(config["site"]["url"]).encode("ascii")


def test_an_example_can_turn_off_the_textarea_requirement():
    """A page with no form still needs to be watchable."""
    config = watch.load_config(watch.ROOT / "examples" / "ticket-drop.yaml")
    assert config["detection"]["require_textarea"] is False


def test_examples_cover_both_a_positive_and_a_negative_utc_offset():
    offsets = {watch.load_config(p)["site"]["timezone_offset_hours"]
               for p in (watch.ROOT / "examples").glob("*.yaml")}
    assert any(o < 0 for o in offsets), "an example should cover a western timezone"


# --- the cron and the config must agree ----------------------------------

def _watch_cron_minutes():
    """Every cron minute the watch workflow schedules itself on."""
    import pathlib
    import re
    text = (pathlib.Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
    crons = re.findall(r"^\s*-\s*cron:\s*'([^']+)'", text, re.MULTILINE)
    assert crons, "watch.yml has no schedule - the watcher would never fire"
    return [int(c.split()[0]) for c in crons]


def test_the_cron_fires_no_earlier_than_the_lookahead():
    """The coupling that silently breaks everything if it drifts.

    watch.py decides which opening a run is waiting for by comparing the
    minute it started against schedule.lookahead_from_minute. If the cron
    fired before that minute, a run starting exactly on time would read
    itself as belonging to the hour just gone, find a deadline already in the
    past, and exit without a single poll - every hour, with nothing in the
    log to say why.
    """
    for minute in _watch_cron_minutes():
        assert minute >= watch.LOOKAHEAD_FROM_MINUTE, (
            "watch.yml fires at :%02d but config.yaml only looks ahead from "
            ":%02d" % (minute, watch.LOOKAHEAD_FROM_MINUTE))


def test_the_cron_leaves_room_for_a_late_scheduler():
    """A window only 15 minutes wide needs more than a few minutes of lead."""
    for minute in _watch_cron_minutes():
        assert 60 - minute >= 15, (
            "watch.yml fires at :%02d, only %d minutes before the opening - "
            "GitHub's scheduler is routinely later than that"
            % (minute, 60 - minute))


def test_the_lookahead_sits_above_the_window_it_closes():
    assert watch.WINDOW_END_MINUTE < watch.LOOKAHEAD_FROM_MINUTE < 60
