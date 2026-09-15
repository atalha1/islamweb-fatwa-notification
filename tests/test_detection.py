"""CLOSED / OPEN / UNKNOWN classification."""
from pathlib import Path

import pytest

import watch

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_closed_page_is_closed():
    state, detail = watch.classify(fixture("closed.html"))
    assert state == watch.STATE_CLOSED
    assert "closed marker" in detail


def test_closed_page_survives_diacritics_tatweel_and_split_tags():
    state, _ = watch.classify(fixture("closed_with_diacritics.html"))
    assert state == watch.STATE_CLOSED


def test_closed_wins_even_if_a_form_is_also_present():
    page = fixture("open.html").replace(
        "<h1>اسأل عن فتوى</h1>",
        "<h1>اسأل عن فتوى</h1><p>نعتذر عن استقبال الأسئلة</p>",
    )
    state, _ = watch.classify(page)
    assert state == watch.STATE_CLOSED


def test_open_page_is_open():
    state, detail = watch.classify(fixture("open.html"))
    assert state == watch.STATE_OPEN
    assert "/ar/fatwa/ask/send" in detail


def test_open_page_with_a_self_posting_form_is_open():
    state, _ = watch.classify(fixture("open_self_post.html"))
    assert state == watch.STATE_OPEN


def test_unknown_when_markup_changed():
    state, detail = watch.classify(fixture("unknown.html"))
    assert state == watch.STATE_UNKNOWN
    assert "no closed marker" in detail


def test_the_site_search_box_is_never_mistaken_for_the_question_form():
    # unknown.html contains only the search form; it must not read as OPEN.
    assert watch.find_question_form(fixture("unknown.html")) is None


def test_a_post_form_without_a_textarea_is_not_the_question_form():
    page = '<form method="post" action="/ar/fatwa/login"><input name="u"></form>'
    assert watch.find_question_form(page) is None


def test_a_post_form_on_an_unrelated_endpoint_is_not_the_question_form():
    page = '<form method="post" action="/ar/newsletter"><textarea></textarea></form>'
    assert watch.find_question_form(page) is None


@pytest.mark.parametrize("page", ["", "   ", "\n"])
def test_empty_body_is_unknown(page):
    state, _ = watch.classify(page)
    assert state == watch.STATE_UNKNOWN


def test_garbage_body_is_unknown():
    state, _ = watch.classify("<html><body>502 Bad Gateway</body></html>")
    assert state == watch.STATE_UNKNOWN


def test_the_live_form_shape_is_detected_despite_having_no_attributes():
    """The real form carries no method and no action; its field names identify it."""
    state, _ = watch.classify(fixture("open_live.html"))
    assert state == watch.STATE_OPEN


def test_one_known_field_name_alone_is_not_enough():
    page = '<form><textarea name="question"></textarea></form>'
    assert watch.find_question_form(page, "https://example.com/unrelated") is None


def test_two_known_field_names_are_enough_even_off_a_fatwa_url():
    page = ('<form><textarea name="question"></textarea>'
            '<input name="guestname"></form>')
    assert watch.find_question_form(page, "https://example.com/unrelated") is not None


def test_a_comment_box_is_not_the_question_form():
    page = ('<form method="post" action="/ar/articles/comment">'
            '<textarea name="comment_body"></textarea>'
            '<input name="commenter"></form>')
    assert watch.find_question_form(page) is None
