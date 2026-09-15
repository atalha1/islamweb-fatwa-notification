"""Answer tracking: turning a number or link into a watched page."""
import pytest

import watch


# --- reference -> URL ----------------------------------------------------

def test_a_bare_number_becomes_a_fatwa_url_with_a_trailing_slash():
    """The trailing slash is load-bearing: without it Islamweb returns
    'no fatwa with this number' even for a fatwa that exists."""
    url = watch.tracking_url("447769")
    assert url == "https://www.islamweb.net/ar/fatwa/447769/"
    assert url.endswith("/")


def test_a_pasted_link_is_used_as_given():
    link = "https://www.islamweb.net/ar/fatwa/447769/المجتهد-وناقل-الفتوى"
    assert watch.tracking_url(link).startswith("https://www.islamweb.net/ar/fatwa/447769/")
    assert watch.tracking_url(link).encode("ascii")  # percent-encoded


def test_a_number_with_noise_around_it_still_works():
    for noisy in (" 447769 ", "رقم 447769", "#447769", "447 769"):
        assert watch.tracking_url(noisy) == "https://www.islamweb.net/ar/fatwa/447769/"


def test_an_http_link_is_not_mangled_into_the_template():
    assert watch.tracking_url("http://example.com/x").startswith("http://example.com/x")


# --- page classification -------------------------------------------------

NOT_FOUND_PAGE = "<html><body><p>لا يوجد فتوي بهذا الرقم</p></body></html>"
ANSWERED_PAGE = ("<html><body><span>447769</span><div>تم نسخ الرابط</div>"
                 "<p>السؤال: ...</p><p>الحمد لله والصلاة والسلام...</p></body></html>")


def test_the_not_found_page_is_not_published():
    result, detail = watch.classify_answer_page(NOT_FOUND_PAGE)
    assert result == watch.TRACK_NOT_PUBLISHED
    assert "not-published" in detail


def test_a_published_fatwa_is_recognised():
    result, detail = watch.classify_answer_page(ANSWERED_PAGE)
    assert result == watch.TRACK_PUBLISHED


def test_not_published_wins_over_a_stray_published_marker():
    """The not-found page still carries the site chrome; a false 'answered'
    alert is far worse than a late one."""
    page = NOT_FOUND_PAGE.replace("</body>", "<span>تم نسخ الرابط</span></body>")
    assert watch.classify_answer_page(page)[0] == watch.TRACK_NOT_PUBLISHED


def test_an_unrecognisable_page_is_unknown_not_published():
    assert watch.classify_answer_page("<html><body>503</body></html>")[0] == watch.TRACK_UNKNOWN
    assert watch.classify_answer_page("")[0] == watch.TRACK_UNKNOWN


def test_arabic_variants_of_the_not_found_phrase_all_match():
    for phrase in ("لا يوجد فتوي بهذا الرقم", "لا توجد فتوى بهذا الرقم",
                   "لا يوجد فتوى بهذا الرقم"):
        page = "<html><body>%s</body></html>" % phrase
        assert watch.classify_answer_page(page)[0] == watch.TRACK_NOT_PUBLISHED, phrase


# --- which entries get tracked -------------------------------------------

def test_only_sent_entries_with_a_reference_are_tracked():
    entries = [
        {"id": "a", "status": "queued", "fatwa_ref": "1"},      # not sent
        {"id": "b", "status": "sent"},                           # no reference
        {"id": "c", "status": "sent", "fatwa_ref": "447769"},    # tracked
        {"id": "d", "status": "sent", "fatwa_url": "https://x/"},  # tracked
        {"id": "e", "status": "answered", "fatwa_ref": "2"},     # already done
    ]
    assert [e["id"] for e in watch.tracked_entries(entries)] == ["c", "d"]


def test_nothing_to_track_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: [])
    monkeypatch.setattr(watch, "STATE_PATH", tmp_path / "state.json")
    assert watch.check_answers(dict(watch.DEFAULT_STATE), force=True) == 0


# --- the full check ------------------------------------------------------

class FakeResponse:
    def __init__(self, body, status=200):
        self.content = body.encode("utf-8")
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}
        self.encoding = "ISO-8859-1"
        self.text = self.content.decode("ISO-8859-1")


def test_a_published_answer_notifies_and_marks_the_entry(monkeypatch, tmp_path):
    entries = [{"id": "q-001", "status": "sent", "title": "T", "fatwa_ref": "447769"}]
    saved, sent = {}, []
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "save_queue", lambda e, *a, **k: saved.update({"e": e}))
    monkeypatch.setattr(watch, "notify", lambda text, state, **k: sent.append(text) or True)
    monkeypatch.setattr(watch, "make_session",
                        lambda: type("S", (), {"get": lambda self, *a, **k:
                                               FakeResponse(ANSWERED_PAGE)})())
    monkeypatch.setattr(watch.time, "sleep", lambda s: None)

    assert watch.check_answers(dict(watch.DEFAULT_STATE), force=True) == 1
    assert entries[0]["status"] == "answered"
    assert entries[0]["answered_at"]
    assert entries[0]["fatwa_url"] == "https://www.islamweb.net/ar/fatwa/447769/"
    assert saved["e"] is entries
    assert "answered" in sent[0].lower()
    assert "447769" in sent[0]


def test_an_unpublished_answer_changes_nothing_and_says_nothing(monkeypatch):
    entries = [{"id": "q-001", "status": "sent", "title": "T", "fatwa_ref": "447769"}]
    sent = []
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "save_queue",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no write")))
    monkeypatch.setattr(watch, "notify", lambda text, state, **k: sent.append(text) or True)
    monkeypatch.setattr(watch, "make_session",
                        lambda: type("S", (), {"get": lambda self, *a, **k:
                                               FakeResponse(NOT_FOUND_PAGE)})())

    assert watch.check_answers(dict(watch.DEFAULT_STATE), force=True) == 0
    assert entries[0]["status"] == "sent"
    assert sent == []


def test_a_network_error_while_tracking_is_survivable(monkeypatch):
    import requests
    entries = [{"id": "q-001", "status": "sent", "title": "T", "fatwa_ref": "447769"}]
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)

    class Boom:
        def get(self, *a, **k):
            raise requests.ConnectionError("down")

    monkeypatch.setattr(watch, "make_session", lambda: Boom())
    assert watch.check_answers(dict(watch.DEFAULT_STATE), force=True) == 0
    assert entries[0]["status"] == "sent"


def test_the_check_interval_is_respected_unless_forced(monkeypatch):
    entries = [{"id": "q-001", "status": "sent", "fatwa_ref": "1"}]
    monkeypatch.setattr(watch, "load_queue", lambda *a, **k: entries)
    monkeypatch.setattr(watch, "make_session",
                        lambda: (_ for _ in ()).throw(AssertionError("too soon")))
    state = dict(watch.DEFAULT_STATE)
    state["last_answer_check_utc"] = watch.utc_now().isoformat()
    assert watch.check_answers(state, force=False) == 0


# --- against the real pages ---------------------------------------------
#
# Excerpts taken verbatim from --dump output on 2026-09-15.

REAL_NOT_FOUND = (
    "<html><body>الفتوي اطرح سؤالك الفتاوي الحيه عرض موضوعي فتاوي معاصره "
    "مختارات الفتاوي عن الفتوي لا يوجد فتوي بهذا الرقم بحث عن فتوي يمكنك "
    "البحث عن الفتوي من خلال البريد الالكتروني النصوص رقم السؤال رقم الفتوي "
    "بالبريد الالكتروني العرض الموضوعي</body></html>")

REAL_PUBLISHED = (
    "<html><body>المجتهد وناقل الفتوي الاقضيه والشهادات &gt; الافتاء , 447769 "
    "الرئيسيه الاقضيه والشهادات الافتاء المجتهد وناقل الفتوي 447769 4474 "
    "الاربعاء 15 صفر 1443 ه - 22-9-2021 م تم نسخ الرابط 0 42 السؤال ما "
    "الفرق بين المجتهد وناقل الفتوى</body></html>")


def test_the_real_not_found_page_reads_as_not_published():
    result, _ = watch.classify_answer_page(REAL_NOT_FOUND)
    assert result == watch.TRACK_NOT_PUBLISHED


def test_the_real_published_fatwa_reads_as_published():
    result, detail = watch.classify_answer_page(REAL_PUBLISHED)
    assert result == watch.TRACK_PUBLISHED
    assert "تم نسخ الرابط" in detail


def test_the_not_found_page_contains_the_word_question_so_order_matters():
    """رقم السؤال on the not-found page contains السؤال, a published marker.

    Checking not-published first is what stops that from reading as an
    answer. This test fails if anyone reorders the checks.
    """
    assert "السؤال" in REAL_NOT_FOUND
    assert "السؤال" in watch.PUBLISHED_MARKERS
    assert watch.classify_answer_page(REAL_NOT_FOUND)[0] == watch.TRACK_NOT_PUBLISHED


def test_the_configured_markers_actually_discriminate():
    """Whatever the config says, it must separate these two real pages."""
    assert (watch.classify_answer_page(REAL_PUBLISHED)[0]
            != watch.classify_answer_page(REAL_NOT_FOUND)[0])
