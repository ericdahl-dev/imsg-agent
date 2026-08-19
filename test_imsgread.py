"""Tests for imsgread.

Every blob here is synthesised by `make_blob` in the same shape macOS writes,
so no real message content lives in the repo.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import imsgread
from imsgread import (
    Message,
    ScopeError,
    apple_time,
    decode_attributed_body,
    read_thread,
    resolve_chat,
)

HEADER = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
TRAILER = b"\x86\x84\x02iI\x01\x0e\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86"


def encode_length(n: int) -> bytes:
    if n < 0x81:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\x81" + n.to_bytes(2, "little")
    return b"\x82" + n.to_bytes(4, "little")


def make_blob(text: str) -> bytes:
    body = text.encode("utf-8")
    return HEADER + encode_length(len(body)) + body + TRAILER


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------

def test_decodes_short_message():
    assert decode_attributed_body(make_blob("yea XF is good")) == "yea XF is good"


def test_decodes_message_at_single_byte_boundary():
    text = "x" * 0x80
    assert decode_attributed_body(make_blob(text)) == text


def test_decodes_long_message_via_uint16_length():
    text = "the coin is cleaned. " * 60  # well past 255 bytes
    assert decode_attributed_body(make_blob(text)) == text


def test_decodes_multibyte_characters():
    text = "graded 65 — worth it \U0001f600"
    assert decode_attributed_body(make_blob(text)) == text


def test_returns_none_for_empty_and_garbage():
    assert decode_attributed_body(None) is None
    assert decode_attributed_body(b"") is None
    assert decode_attributed_body(b"not a typedstream") is None


def test_returns_none_when_length_overruns_buffer():
    truncated = HEADER + encode_length(500) + b"short"
    assert decode_attributed_body(truncated) is None


def test_takes_the_first_nsstring_not_an_attribute_string():
    blob = make_blob("real body") + b"\x84\x84\x08NSString\x01\x94\x84\x01+\x09attribute"
    assert decode_attributed_body(blob) == "real body"


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------

def test_apple_time_handles_nanoseconds():
    # 2026-08-19T00:00:00Z expressed as ns since 2001-01-01
    raw = int((datetime(2026, 8, 19, tzinfo=timezone.utc) - imsgread.APPLE_EPOCH).total_seconds() * 1e9)
    assert apple_time(raw) == datetime(2026, 8, 19, tzinfo=timezone.utc)


def test_apple_time_handles_legacy_seconds():
    raw = int((datetime(2012, 3, 4, tzinfo=timezone.utc) - imsgread.APPLE_EPOCH).total_seconds())
    assert apple_time(raw) == datetime(2012, 3, 4, tzinfo=timezone.utc)


def test_apple_time_of_zero_is_none():
    assert apple_time(0) is None
    assert apple_time(None) is None


# --------------------------------------------------------------------------
# scoping -- the safety property
# --------------------------------------------------------------------------

def test_resolve_chat_requires_a_target():
    with pytest.raises(ScopeError):
        resolve_chat(None, None)


def test_resolve_chat_passes_through_explicit_id():
    assert resolve_chat("+15555550123", None) == "+15555550123"


def test_resolve_chat_rejects_unknown_contact(monkeypatch):
    monkeypatch.setattr(imsgread, "load_contacts", lambda *a, **k: {"disco": "+1555"})
    with pytest.raises(ScopeError, match="unknown contact"):
        resolve_chat(None, "nobody")


def test_read_thread_refuses_empty_chat_id():
    with pytest.raises(ScopeError):
        read_thread("", db_path=Path("/nonexistent"))


# --------------------------------------------------------------------------
# reading, against a synthetic database
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "chat.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table message (ROWID integer primary key, date integer, is_from_me integer,
                              text text, attributedBody blob, cache_has_attachments integer);
        create table chat (ROWID integer primary key, chat_identifier text);
        create table chat_message_join (chat_id integer, message_id integer);
        """
    )
    conn.execute("insert into chat values (1, '+15555550123')")
    conn.execute("insert into chat values (2, '+15555559999')")

    def add(rowid, chat, date, from_me, text, blob, attach=0):
        conn.execute(
            "insert into message values (?,?,?,?,?,?)", (rowid, date, from_me, text, blob, attach)
        )
        conn.execute("insert into chat_message_join values (?,?)", (chat, rowid))

    base = 700_000_000 * 10**9
    add(1, 1, base + 1, 0, None, make_blob("first from them"))
    add(2, 1, base + 2, 1, "plain text column wins", None)
    add(3, 1, base + 3, 0, None, None, attach=1)          # attachment only
    add(4, 1, base + 4, 0, None, None)                    # empty, should drop
    add(5, 2, base + 5, 0, None, make_blob("OTHER THREAD"))
    conn.commit()
    conn.close()
    return path


def test_reads_only_the_requested_thread(db):
    msgs = read_thread("+15555550123", db_path=db)
    assert all("OTHER THREAD" != (m.text or "") for m in msgs)
    assert len(msgs) == 3  # empty row dropped, attachment kept


def test_prefers_text_column_then_falls_back_to_blob(db):
    msgs = {m.text for m in read_thread("+15555550123", db_path=db)}
    assert "plain text column wins" in msgs
    assert "first from them" in msgs


def test_newest_first_and_direction(db):
    msgs = read_thread("+15555550123", db_path=db)
    assert msgs[0].has_attachment is True          # rowid 3 is newest surviving
    assert any(m.from_me for m in msgs)


def test_include_empty_keeps_bodyless_rows(db):
    assert len(read_thread("+15555550123", db_path=db, include_empty=True)) == 4


def test_limit_is_applied(db):
    assert len(read_thread("+15555550123", db_path=db, limit=1)) <= 1


def test_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_thread("+15555550123", db_path=tmp_path / "nope.db")


def test_render_is_readable():
    m = Message(date="2026-08-19T14:30:00+00:00", from_me=False, text="hi", has_attachment=True)
    assert m.render() == "2026-08-19 14:30  them  hi [attachment]"
