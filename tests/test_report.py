"""The observation report - the whole point of collecting the log."""
import watch


def row(date, hour, minute, state, status=200):
    stamp = "%s T%02d:%02d:00+03:00".replace(" ", "") % (date, hour, minute)
    return "2026-09-15T00:00:00Z,%s,%d,%s" % (stamp, status, state)


def write_log(tmp_path, rows):
    path = tmp_path / "observations.csv"
    path.write_text("timestamp_utc,timestamp_local,http_status,state\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_an_empty_log_reports_honestly_instead_of_crashing(tmp_path):
    path = write_log(tmp_path, [])
    assert watch.read_observations(path) == []
    assert "No observations logged yet" in watch.build_report([])


def test_malformed_rows_are_skipped_not_fatal(tmp_path):
    path = write_log(tmp_path, [
        row("2026-09-16", 10, 0, "OPEN"),
        "garbage,,,",
        "2026-09-16T00:00:00Z,not-a-timestamp,200,OPEN",
        "2026-09-16T00:00:00Z,2026-09-16T10:01:00+03:00,200,NONSENSE",
        row("2026-09-16", 10, 1, "OPEN"),
    ])
    rows = watch.read_observations(path)
    assert len(rows) == 2
    assert all(r["state"] == "OPEN" for r in rows)


def test_the_report_answers_which_hours_open(tmp_path):
    rows = []
    # 10:00 opens wide; 11:00 never opens; 12:00 opens for one poll only.
    for minute in (0, 1, 2):
        rows.append(row("2026-09-16", 10, minute, "OPEN"))
    for minute in (0, 1, 2):
        rows.append(row("2026-09-16", 11, minute, "CLOSED"))
    rows.append(row("2026-09-16", 12, 0, "OPEN"))
    rows.append(row("2026-09-16", 12, 1, "CLOSED"))

    report = watch.build_report(watch.read_observations(write_log(tmp_path, rows)))

    assert "| 10:00 | 3 | 3 | 0 | 0 | 100% |" in report
    assert "| 11:00 | 3 | 0 | 3 | 0 | 0% |" in report
    assert "Hours ever seen open:** 10:00, 12:00" in report
    assert "Hours polled but never seen open:** 11:00" in report


def test_the_report_estimates_how_long_a_window_stays_open(tmp_path):
    rows = [row("2026-09-16", 10, minute, "OPEN") for minute in (0, 1, 2)]
    report = watch.build_report(watch.read_observations(write_log(tmp_path, rows)))
    assert "+0m00s" in report and "+2m00s" in report
    assert "~2m20s" in report  # 2 minutes spanned, plus one poll interval


def test_window_duration_uses_seconds_not_just_minutes(tmp_path):
    """Three polls 20s apart are a ~1 minute window, not a zero-minute one."""
    path = tmp_path / "obs.csv"
    path.write_text(
        "timestamp_utc,timestamp_local,http_status,state\n"
        + "".join("2026-09-16T07:00:%02dZ,2026-09-16T10:00:%02d+03:00,200,OPEN\n"
                  % (sec, sec) for sec in (0, 20, 40)),
        encoding="utf-8")
    rows = watch.read_observations(path)
    assert [r["offset"] for r in rows] == [0, 20, 40]
    report = watch.build_report(rows)
    assert "~1m00s" in report


def test_the_report_flags_a_run_of_unknown_readings(tmp_path):
    rows = [row("2026-09-16", 10, m, "UNKNOWN") for m in range(3)]
    report = watch.build_report(watch.read_observations(write_log(tmp_path, rows)))
    assert "UNKNOWN readings" in report
    assert "--dump" in report
    assert "3 of 3 polls" in report


def test_the_report_counts_days_and_names_the_site(tmp_path):
    rows = [row("2026-09-16", 10, 0, "OPEN"), row("2026-09-17", 10, 0, "CLOSED")]
    report = watch.build_report(watch.read_observations(write_log(tmp_path, rows)))
    assert "Days covered: **2** (2026-09-16 to 2026-09-17)" in report
    assert watch.SITE_NAME in report
    assert "Polls logged: **2**" in report


def test_the_legacy_timestamp_makkah_column_is_still_readable(tmp_path):
    """Logs written before the column was renamed must not become unreadable."""
    path = tmp_path / "old.csv"
    path.write_text("timestamp_utc,timestamp_makkah,http_status,state\n"
                    "2026-09-16T07:00:00Z,2026-09-16T10:00:00+03:00,200,OPEN\n",
                    encoding="utf-8")
    rows = watch.read_observations(path)
    assert len(rows) == 1 and rows[0]["hour"] == 10


def test_the_report_renders_from_the_shipped_empty_log():
    assert "Observation report" in watch.build_report(watch.read_observations())
