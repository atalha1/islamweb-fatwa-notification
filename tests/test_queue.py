"""Queue selection order and the shipped placeholder queue."""
import watch


def test_lowest_priority_number_is_picked_first():
    entries = [
        {"id": "c", "priority": 9, "status": "queued"},
        {"id": "a", "priority": 1, "status": "queued"},
        {"id": "b", "priority": 5, "status": "queued"},
    ]
    assert watch.select_next_question(entries)["id"] == "a"


def test_sent_and_answered_entries_are_skipped():
    entries = [
        {"id": "a", "priority": 1, "status": "sent"},
        {"id": "b", "priority": 2, "status": "answered"},
        {"id": "c", "priority": 3, "status": "queued"},
    ]
    assert watch.select_next_question(entries)["id"] == "c"


def test_ties_are_broken_by_id_so_selection_is_deterministic():
    entries = [
        {"id": "q-002", "priority": 1, "status": "queued"},
        {"id": "q-001", "priority": 1, "status": "queued"},
    ]
    assert watch.select_next_question(entries)["id"] == "q-001"


def test_empty_or_fully_sent_queue_returns_none():
    assert watch.select_next_question([]) is None
    assert watch.select_next_question([{"id": "a", "priority": 1, "status": "sent"}]) is None


def test_status_matching_is_case_insensitive():
    assert watch.select_next_question([{"id": "a", "priority": 1, "status": "QUEUED"}])["id"] == "a"


def test_a_missing_or_bad_priority_sorts_last_without_crashing():
    entries = [
        {"id": "bad", "priority": "oops", "status": "queued"},
        {"id": "none", "status": "queued"},
        {"id": "good", "priority": 4, "status": "queued"},
    ]
    assert watch.select_next_question(entries)["id"] == "good"


def test_the_shipped_queue_file_is_valid_and_selectable():
    entries = watch.load_queue()
    assert len(entries) == 3
    for entry in entries:
        assert set(watch.QUEUE_FIELD_ORDER) <= set(entry)
    assert watch.select_next_question(entries)["id"] == "q-001"


def test_selected_entry_renders_into_a_message():
    entry = watch.select_next_question(watch.load_queue())
    message = watch.compose_open_message(entry)
    assert "q-001" in message
    assert "--mark-sent q-001" in message
    assert watch.FATWA_PAGE_URL in message


def test_message_is_still_useful_when_nothing_is_queued():
    message = watch.compose_open_message(None)
    assert "No queued question" in message


def test_saving_the_queue_keeps_the_header_comments(tmp_path):
    """Regression: yaml.safe_dump dropped the usage notes on the first --mark-sent."""
    path = tmp_path / "queue.yaml"
    path.write_text(
        "# How to use this file.\n"
        "# Run --mark-sent after you submit.\n"
        "\n"
        "- id: q-001\n  priority: 1\n  status: queued\n",
        encoding="utf-8",
    )
    entries = watch.load_queue(path)
    entries[0]["status"] = "sent"
    watch.save_queue(entries, path)

    written = path.read_text(encoding="utf-8")
    assert written.startswith("# How to use this file.\n# Run --mark-sent after you submit.\n")
    assert watch.load_queue(path)[0]["status"] == "sent"


def test_round_tripping_the_real_queue_preserves_its_header_and_arabic(tmp_path):
    path = tmp_path / "queue.yaml"
    path.write_text(watch.QUEUE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    original_header = path.read_text(encoding="utf-8").split("\n- id:")[0]
    watch.save_queue(watch.load_queue(path), path)
    written = path.read_text(encoding="utf-8")
    assert written.startswith(original_header.rstrip("\n").rstrip())
    assert "سؤال تجريبي رقم واحد" in written
    assert [e["id"] for e in watch.load_queue(path)] == ["q-001", "q-002", "q-003"]
