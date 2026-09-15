"""Arabic text must survive the trip through the CallMeBot query string."""
from urllib.parse import parse_qs, unquote, urlsplit

import watch

ARABIC = "ما حكم الصلاة في الطائرة؟ وهل تُقصر؟ — سؤال رقم ١"


def test_arabic_round_trips_through_the_whatsapp_url():
    url = watch.build_whatsapp_url("+9715000000", ARABIC, "123456")
    # The raw URL must be pure ASCII percent-encoding - no raw UTF-8 bytes.
    url.encode("ascii")
    assert "%D9" in url or "%d9" in url  # Arabic really was percent-encoded
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert params["text"] == [ARABIC]
    assert params["phone"] == ["+9715000000"]
    assert params["apikey"] == ["123456"]


def test_multiline_arabic_message_round_trips():
    entry = {
        "id": "q-001",
        "title": "سؤال تجريبي",
        "body": "نص السؤال الكامل هنا. " * 40,
    }
    message = watch.compose_open_message(entry)
    url = watch.build_whatsapp_url("+100", message, "key")
    url.encode("ascii")
    assert parse_qs(urlsplit(url).query)["text"] == [message]


def test_newlines_and_ampersands_do_not_break_the_query_string():
    tricky = "سطر أول\nسطر ثانٍ & ثالث = رابع?"
    url = watch.build_whatsapp_url("+100", tricky, "key")
    assert parse_qs(urlsplit(url).query)["text"] == [tricky]
    assert parse_qs(urlsplit(url).query)["apikey"] == ["key"]


def test_body_preview_is_capped_at_300_characters():
    entry = {"id": "x", "title": "t", "body": "ب" * 1000}
    message = watch.compose_open_message(entry)
    assert "ب" * watch.BODY_PREVIEW_CHARS in message
    assert "ب" * (watch.BODY_PREVIEW_CHARS + 1) not in message
    assert "…" in message


def test_page_url_is_percent_encoded_ascii():
    watch.FATWA_PAGE_URL.encode("ascii")
    assert unquote(watch.FATWA_PAGE_PATH) == "/ar/fatwa/اسأل-عن-فتوى"
