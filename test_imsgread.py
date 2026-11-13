"""Tests for imsgread.

Every blob here is synthesised by `make_blob` in the same shape macOS writes,
so no real message content lives in the repo.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent

import imsgread
from imsgread import (
    main,
    Message,
    ScopeError,
    apple_time,
    decode_attributed_body,
    resolve_chat,
)

@pytest.fixture(autouse=True)
def no_real_chat_db(tmp_path, monkeypatch):
    """No test may reach ~/Library/Messages/chat.db. It holds every
    conversation on the machine; a test that forgets --db must fail loudly
    rather than quietly read real messages."""
    monkeypatch.setattr(imsgread, "DEFAULT_DB", tmp_path / "no-real-chat.db")


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
    monkeypatch.setattr(imsgread, "load_contacts", lambda *a, **k: {"friend": "+1555"})
    with pytest.raises(ScopeError, match="unknown contact"):
        resolve_chat(None, "nobody")


def test_read_thread_refuses_empty_chat_id():
    with pytest.raises(ScopeError):
        imsgread.ChatStore(Path("/nonexistent")).read_thread("")


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
                              text text, attributedBody blob, cache_has_attachments integer,
                              handle_id integer);
        create table chat (ROWID integer primary key, chat_identifier text, guid text,
                           display_name text);
        create table chat_message_join (chat_id integer, message_id integer);
        create table handle (ROWID integer primary key, id text);
        create table chat_handle_join (chat_id integer, handle_id integer);
        create table attachment (ROWID integer primary key, filename text,
                                 mime_type text, total_bytes integer);
        create table message_attachment_join (message_id integer, attachment_id integer);
        """
    )
    # one attachment that is still on disk, one that has been cleaned up
    real = tmp_path / "pic.jpg"
    real.write_bytes(b"x" * 2_202_010)
    conn.execute("insert into attachment values (1, ?, 'image/jpeg', 2202010)", (str(real),))
    conn.execute("insert into attachment values (2, '~/gone/old.png', 'image/png', 4096)")
    # a link preview: Messages writes one per URL and they are never openable
    conn.execute("insert into attachment values (3, '~/p/AB12.pluginPayloadAttachment', null, 2048)")
    conn.execute("insert into message_attachment_join values (3, 1)")
    conn.execute("insert into message_attachment_join values (3, 2)")
    conn.execute("insert into message_attachment_join values (3, 3)")
    conn.execute("insert into chat values (1, '+15555550123', 'iMessage;-;+15555550123', null)")
    conn.execute("insert into chat values (2, '+15555559999', 'iMessage;-;+15555559999', null)")
    conn.execute("insert into chat values (3, 'chat9001', 'iMessage;+;chat9001', 'trail crew')")
    conn.execute("insert into chat values (4, 'chat9002', null, 'no guid')")
    for rowid, ident in ((1, "+15555550123"), (2, "+15555559999"), (3, "+15555551111")):
        conn.execute("insert into handle values (?,?)", (rowid, ident))
    # one-to-one chats have a single participant; the group has three
    for chat, handle in ((1, 1), (2, 2), (3, 1), (3, 2), (3, 3), (4, 1), (4, 2)):
        conn.execute("insert into chat_handle_join values (?,?)", (chat, handle))

    def add(rowid, chat, date, from_me, text, blob, attach=0, handle_id=None):
        conn.execute(
            "insert into message values (?,?,?,?,?,?,?)",
            (rowid, date, from_me, text, blob, attach, handle_id),
        )
        conn.execute("insert into chat_message_join values (?,?)", (chat, rowid))

    base = 700_000_000 * 10**9
    add(1, 1, base + 1, 0, None, make_blob("first from them"), handle_id=1)
    add(2, 1, base + 2, 1, "plain text column wins", None)
    add(3, 1, base + 3, 0, None, None, attach=1, handle_id=1)   # attachment only
    add(4, 1, base + 4, 0, None, None, handle_id=1)             # empty, should drop
    add(5, 2, base + 5, 0, None, make_blob("OTHER THREAD"), handle_id=2)
    add(6, 3, base + 6, 0, "from alice", None, handle_id=1)
    add(7, 3, base + 7, 0, "from carol", None, handle_id=3)
    add(8, 3, base + 8, 1, "from me", None)
    conn.commit()
    conn.close()
    return path


def test_reads_only_the_requested_thread(db):
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    assert all("OTHER THREAD" != (m.text or "") for m in msgs)
    assert len(msgs) == 3  # empty row dropped, attachment kept


def test_prefers_text_column_then_falls_back_to_blob(db):
    msgs = {m.text for m in imsgread.ChatStore(db).read_thread("+15555550123")}
    assert "plain text column wins" in msgs
    assert "first from them" in msgs


def test_newest_first(db):
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    assert msgs[0].has_attachment is True          # rowid 3 is newest surviving


def test_returns_both_sides_of_the_conversation(db):
    """Both directions must come back. Reading only the other side loses what
    you already said -- and therefore what is still unanswered."""
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    assert any(m.from_me for m in msgs), "lost the sent side"
    assert any(not m.from_me for m in msgs), "lost the received side"


def test_direction_is_not_inverted(db):
    """rowid 2 is the only is_from_me=1 row in the fixture."""
    sent = [m for m in imsgread.ChatStore(db).read_thread("+15555550123") if m.from_me]
    assert [m.text for m in sent] == ["plain text column wins"]


def test_include_empty_keeps_bodyless_rows(db):
    assert len(imsgread.ChatStore(db).read_thread("+15555550123", include_empty=True)) == 4


def test_limit_is_applied(db):
    assert len(imsgread.ChatStore(db).read_thread("+15555550123", limit=1)) <= 1


def test_render_is_readable():
    m = Message(date="2026-08-19T14:30:00+00:00", from_me=False, text="hi", has_attachment=True)
    assert m.render() == "2026-08-19 14:30  them  hi [attachment]"


def test_a_group_thread_attributes_each_sender(db):
    """Collapsing a group into one word makes the transcript quotable but
    misattributable, which is worse than not reading it at all."""
    msgs = imsgread.ChatStore(db).read_thread("chat9001")
    said = {m.sender: m.text for m in msgs if not m.from_me}
    assert len(said) == 2, said
    assert set(said.values()) == {"from alice", "from carol"}
    assert all(m.sender is None for m in msgs if m.from_me)


def test_a_one_to_one_thread_keeps_its_shape_and_hides_the_number(db):
    """The whole point of contacts.toml is that numbers stay out of output."""
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    assert all(m.sender is None for m in msgs)
    rendered = "\n".join(m.render() for m in msgs)
    assert "+15555550123" not in rendered
    assert "them" in rendered


def test_group_senders_use_a_known_name_and_mask_the_rest(db):
    msgs = imsgread.ChatStore(db).read_thread("chat9001",
                       labels={"+15555550123": "alice"})
    senders = {m.sender for m in msgs if not m.from_me}
    assert "alice" in senders
    assert "*1111" in senders          # unnamed participant, masked
    assert "+15555551111" not in senders


def test_an_attachment_reports_its_type_size_and_path(db):
    """[attachment] says something arrived but not what. Type, size and path
    are what make a transcript actionable."""
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    shot = next(m for m in msgs if m.has_attachment)
    kept = next(a for a in shot.attachments if a.exists)

    assert kept.mime_type == "image/jpeg"
    assert kept.total_bytes == 2202010
    rendered = shot.render()
    assert "image/jpeg" in rendered
    assert "2.1 MB" in rendered
    assert kept.path in rendered


def test_an_attachment_no_longer_on_disk_is_marked_missing(db):
    """About one in eight recent attachments is already gone; a read must
    treat that as normal rather than as an error."""
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    shot = next(m for m in msgs if m.has_attachment)
    gone = next(a for a in shot.attachments if not a.exists)

    assert gone.mime_type == "image/png"
    assert "missing" in shot.render()


def test_a_message_without_an_attachment_is_unchanged(db):
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    plain = next(m for m in msgs if m.text == "plain text column wins")
    assert plain.attachments == []
    assert plain.render().endswith("plain text column wins")


# --- sending -----------------------------------------------------------------

def test_agent_marker_is_appended_when_requested():
    from imsgread import mark_agent_sent

    assert mark_agent_sent("Photos are fixed", robot=True) == "Photos are fixed 🤖"


def test_text_is_untouched_when_not_marking():
    from imsgread import mark_agent_sent

    assert mark_agent_sent("Photos are fixed", robot=False) == "Photos are fixed"


def test_trailing_newline_from_a_message_file_never_reaches_the_recipient():
    from imsgread import mark_agent_sent

    # Drafts arrive from --message-file, and files end in a newline. Sending it
    # shows up as a blank line on the recipient's phone.
    assert mark_agent_sent("Photos are fixed\n", robot=False) == "Photos are fixed"
    assert mark_agent_sent("Photos are fixed\n", robot=True) == "Photos are fixed \U0001F916"
    assert mark_agent_sent("Photos are fixed \U0001F916\n", robot=True) == "Photos are fixed \U0001F916"


def test_the_group_script_never_brings_messages_to_the_front():
    """Same requirement as the 1:1 script, pinned separately so a new script
    cannot quietly reintroduce focus stealing."""
    assert "activate" not in imsgread.CHAT_SEND_SCRIPT
    assert "launch" in imsgread.CHAT_SEND_SCRIPT


def test_a_group_with_no_guid_is_refused_rather_than_guessed(messages, send_db, capsys):
    code = main(["--send", "--chat", "chat9002", "--message", "hi", "--confirm-timeout", "0",
                 "--robot", "--db", str(send_db)])

    assert code != 0
    assert "group" in capsys.readouterr().err.lower()
    assert messages.calls == []


def test_non_interactive_send_must_state_the_marker_choice(messages, send_db, monkeypatch, capsys):
    """An agent cannot send unmarked by omission."""
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    code = main(["--send", "--chat", "+15555550123", "--message", "hi", "--confirm-timeout", "0",
                 "--db", str(send_db)])

    assert code != 0
    assert "--robot" in capsys.readouterr().err
    assert messages.calls == []


def test_non_interactive_send_proceeds_when_told(messages, send_db, monkeypatch):
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(["--send", "--chat", "+15555550123", "--message", "hi", "--robot",
                 "--confirm-timeout", "0", "--db", str(send_db)]) == 0
    assert messages.calls == [("chat", "any;-;+15555550123", "hi \U0001F916")]


def test_interactive_send_asks_before_marking(messages, send_db, monkeypatch):
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    assert main(["--send", "--chat", "+15555550123", "--message", "hi", "--confirm-timeout", "0",
                 "--db", str(send_db)]) == 0
    assert messages.calls[-1][2] == "hi"


# --- Claude skill ------------------------------------------------------------

def test_claude_skill_documents_safe_scoped_usage():
    skill = REPO_ROOT / ".claude" / "skills" / "imsg-agent" / "SKILL.md"
    body = skill.read_text(encoding="utf-8")

    required = [
        "name: imsg-agent",
        # the description is what makes the skill fire without a slash command,
        # so it has to carry the words a person actually uses
        "text someone",
        "check what someone said",
        # a name is not an identifier: resolve it, never guess which number
        "imsg-agent --contacts",
        "never guess which identifier a name refers to",
        "Always scope every read or send to one `--contact` or one `--chat` identifier.",
        "Prefer `--contact CONTACT_NAME` from `contacts.toml`",
        "`CONTACT_NAME` is a configured contact alias, not a literal name to copy.",
        "Do not query `~/Library/Messages/chat.db` directly",
        "use `--robot` so the outgoing message is marked with 🤖",
        # the marker is about authorship, not mechanism: a human's approved words
        # go unmarked even though an agent typed them
        "The marker reflects whose words they are, not who typed them.",
        "only for words a human wrote or approved as their own",
        # the marker default is the one value allowed to answer for a program,
        # so an agent writing it would be granting itself permission
        "Never write that setting yourself.",
        "refuses to run without a terminal",
        # granting Full Disk Access does not reach an already-running process
        "restart the session afterwards",
        # UTC output has been misread as local time
        "Timestamps are printed in UTC.",
        "Prefer `--message-file`",
        # commands must be the installed executable, not a path relative to
        # the repo root -- the skill is used from other projects' directories
        "imsg-agent --contact CONTACT_NAME -n 20 --text",
        "imsg-agent --send --contact CONTACT_NAME --message-file reply.txt --robot",
        "they work from any working directory",
        "command not found",
    ]
    for text in required:
        assert text in body

    assert "python3 imsgread.py" not in body, (
        "skill commands must not be repo-relative: they break from any other cwd"
    )


# --- contacts file lookup ----------------------------------------------------

def write_contacts(path: Path, name: str, ident: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'[contacts]\n{name} = "{ident}"\n', encoding="utf-8")
    return path


def test_contacts_env_var_wins_over_config_dir(tmp_path, monkeypatch):
    env_file = write_contacts(tmp_path / "env.toml", "fromenv", "+1555")
    config = write_contacts(tmp_path / "config.toml", "fromconfig", "+1666")
    monkeypatch.setenv(imsgread.CONTACTS_ENV, str(env_file))
    monkeypatch.setattr(imsgread, "CONFIG_CONTACTS", config)

    assert imsgread.contacts_path() == env_file
    assert imsgread.load_contacts() == {"fromenv": "+1555"}


def test_contacts_falls_back_to_config_dir(tmp_path, monkeypatch):
    config = write_contacts(tmp_path / "config.toml", "fromconfig", "+1666")
    monkeypatch.delenv(imsgread.CONTACTS_ENV, raising=False)
    monkeypatch.setattr(imsgread, "CONFIG_CONTACTS", config)
    monkeypatch.setattr(imsgread, "BUNDLED_CONTACTS", tmp_path / "absent.toml")

    assert imsgread.contacts_path() == config
    assert imsgread.load_contacts() == {"fromconfig": "+1666"}


def test_contacts_falls_back_to_file_beside_module(tmp_path, monkeypatch):
    """A clone or editable install keeps contacts.toml in the repo root."""
    bundled = write_contacts(tmp_path / "bundled.toml", "frombundle", "+1777")
    monkeypatch.delenv(imsgread.CONTACTS_ENV, raising=False)
    monkeypatch.setattr(imsgread, "CONFIG_CONTACTS", tmp_path / "absent.toml")
    monkeypatch.setattr(imsgread, "BUNDLED_CONTACTS", bundled)

    assert imsgread.contacts_path() == bundled
    assert imsgread.load_contacts() == {"frombundle": "+1777"}


def test_contacts_path_points_at_config_dir_when_nothing_exists(tmp_path, monkeypatch):
    """The path suggested to the user must be one they can actually write to,
    never a site-packages directory an upgrade would wipe."""
    config = tmp_path / "config" / "imsg-agent" / "contacts.toml"
    monkeypatch.delenv(imsgread.CONTACTS_ENV, raising=False)
    monkeypatch.setattr(imsgread, "CONFIG_CONTACTS", config)
    monkeypatch.setattr(imsgread, "BUNDLED_CONTACTS", tmp_path / "absent.toml")

    assert imsgread.contacts_path() == config
    assert imsgread.load_contacts() == {}


def test_contacts_file_flag_overrides_every_default(tmp_path, monkeypatch):
    explicit = write_contacts(tmp_path / "explicit.toml", "friend", "+1888")
    monkeypatch.setenv(imsgread.CONTACTS_ENV, str(write_contacts(
        tmp_path / "env.toml", "other", "+1999")))

    assert imsgread.resolve_chat(None, "friend", explicit) == "+1888"


def test_resolve_chat_honors_contacts_file_from_cli(tmp_path, messages, send_db):
    explicit = write_contacts(tmp_path / "explicit.toml", "friend", "+1888")

    assert imsgread.main([
        "--send", "--contact", "friend", "--message", "hi", "--robot",
        "--contacts-file", str(explicit), "--confirm-timeout", "0", "--db", str(send_db),
    ]) == 0
    assert messages.calls[-1][1] == "+1888"


def test_send_script_never_brings_messages_to_the_front():
    """Sending must not steal focus. `activate` does; `launch` starts Messages
    in the background when it is not already running. There is no way to assert
    this without a GUI, so the requirement is pinned on the script itself."""
    assert "activate" not in imsgread.SEND_SCRIPT
    assert "launch" in imsgread.SEND_SCRIPT


# --- per-contact marker default ----------------------------------------------

def write_toml(path: Path, body: str) -> Path:
    """write_contacts only emits the plain form; these need the table form too."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def send_args(cfg: Path, db: Path, *extra: str) -> list[str]:
    # --confirm-timeout 0: the fake transport writes its row synchronously, so
    # there is nothing to wait for.
    return ["--send", "--contact", "eric", "--message", "hi",
            "--contacts-file", str(cfg), "--db", str(db), "--confirm-timeout", "0", *extra]


def was_marked(fake) -> bool:
    return fake.calls[-1][2].endswith("\U0001F916")


def test_both_contact_forms_load_from_one_file(tmp_path):
    cfg = write_toml(tmp_path / "c.toml", (
        '[contacts]\n'
        'brian = "+1555"\n'
        'eric = { id = "+1666", marker = false }\n'
        'mom = { id = "+1777", marker = true }\n'
    ))
    entries = imsgread.load_contact_entries(cfg)

    assert entries["brian"] == imsgread.Contact("+1555", None)
    assert entries["eric"] == imsgread.Contact("+1666", False)
    assert entries["mom"] == imsgread.Contact("+1777", True)
    # the old contract is what every other caller still uses
    assert imsgread.load_contacts(cfg) == {"brian": "+1555", "eric": "+1666", "mom": "+1777"}


@pytest.mark.parametrize("body, fragment", [
    ('[contacts]\neric = { marker = false }\n', "no 'id'"),
    ('[contacts]\neric = { id = "+1", marekr = false }\n', "unknown key"),
    ('[contacts]\neric = { id = "+1", marker = "never" }\n', "non-boolean"),
    ('[contacts]\neric = 12\n', "must be an identifier or a table"),
])
def test_a_malformed_contact_is_loud(tmp_path, body, fragment):
    """A silently ignored marker is the worst outcome: the caller believes a
    standing decision is in force while every send does the opposite."""
    cfg = write_toml(tmp_path / "c.toml", body)
    with pytest.raises(imsgread.ConfigError) as exc:
        imsgread.load_contact_entries(cfg)
    assert fragment in str(exc.value)
    assert "eric" in str(exc.value)


@pytest.mark.parametrize("stored, expected", [("false", False), ("true", True)])
def test_a_contact_default_answers_for_a_program(tmp_path, messages, send_db, monkeypatch, stored, expected):
    """The one behavior change: silence now resolves to a decision a person
    recorded, instead of being refused."""
    cfg = write_toml(tmp_path / "c.toml",
                     f'[contacts]\neric = {{ id = "+1555", marker = {stored} }}\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(send_args(cfg, send_db)) == 0
    assert messages.calls[-1][1] == "+1555"
    assert was_marked(messages) is expected


@pytest.mark.parametrize("stored, flag, expected", [
    ("false", "--robot", True),
    ("true", "--no-robot", False),
])
def test_an_explicit_flag_beats_the_contact_default(tmp_path, messages, send_db, monkeypatch, stored, flag, expected):
    cfg = write_toml(tmp_path / "c.toml",
                     f'[contacts]\neric = {{ id = "+1555", marker = {stored} }}\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(send_args(cfg, send_db, flag)) == 0
    assert was_marked(messages) is expected


def test_a_contact_without_a_setting_still_refuses(tmp_path, messages, send_db, monkeypatch, capsys):
    """Unchanged behavior for everyone who has not opted in."""
    cfg = write_contacts(tmp_path / "c.toml", "eric", "+1555")
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(send_args(cfg, send_db)) != 0
    assert "--robot" in capsys.readouterr().err
    assert messages.calls == []


def test_a_raw_chat_identifier_ignores_any_contact_default(tmp_path, messages, send_db, monkeypatch, capsys):
    """--chat is not a contact, so there is nowhere a decision could have been
    recorded -- even when the same number is configured."""
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = false }\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(["--send", "--chat", "+1555", "--message", "hi", "--confirm-timeout", "0",
                 "--contacts-file", str(cfg), "--db", str(send_db)]) != 0
    assert "--robot" in capsys.readouterr().err
    assert messages.calls == []


def test_contacts_listing_shows_a_setting_only_when_set(tmp_path, capsys):
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml", (
        '[contacts]\nbrian = "+1555"\neric = { id = "+1666", marker = false }\n'
    ))
    assert main(["--contacts", "--contacts-file", str(cfg)]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == ["brian\t*1555", "eric\t*1666\tmarker=never"]


def test_contacts_listing_masks_identifiers_by_default(tmp_path, capsys):
    """An agent lists contacts to learn the names; the numbers must not ride along."""
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml", (
        '[contacts]\nbrian = "+15555550123"\nmom = "mom@example.com"\n'
    ))
    assert main(["--contacts", "--contacts-file", str(cfg)]) == 0

    out = capsys.readouterr().out
    assert "+15555550123" not in out
    assert "mom@example.com" not in out
    assert out.splitlines() == ["brian\t*0123", "mom\tmo***@example.com"]


def test_contacts_listing_shows_full_identifiers_only_when_asked(tmp_path, capsys):
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml", '[contacts]\nbrian = "+15555550123"\n')
    assert main(["--contacts", "--unmask", "--contacts-file", str(cfg)]) == 0

    assert capsys.readouterr().out.splitlines() == ["brian\t+15555550123"]


# --- writing the config ------------------------------------------------------

def test_writing_a_contact_keeps_the_rest_of_the_file(tmp_path):
    """Rewriting from parsed data would drop a person's own comments."""
    cfg = write_toml(tmp_path / "c.toml", (
        "# my header\n\n[contacts]\n# brian is my brother\n"
        'brian = "+1555"\ndisco = "+1666"\n'
    ))
    imsgread.write_contact("brian", imsgread.Contact("+1555", True), cfg)
    imsgread.write_contact("eric", imsgread.Contact("+1777", False), cfg)

    text = cfg.read_text()
    assert "# my header" in text
    assert "# brian is my brother" in text
    assert 'brian = { id = "+1555", marker = true }' in text
    assert 'disco = "+1666"' in text
    assert imsgread.load_contact_entries(cfg)["eric"] == imsgread.Contact("+1777", False)


def test_clearing_a_setting_returns_the_plain_form(tmp_path):
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = false }\n')
    imsgread.write_contact("eric", imsgread.Contact("+1555", None), cfg)

    assert 'eric = "+1555"' in cfg.read_text()


def test_a_contact_that_would_break_the_file_is_refused(tmp_path):
    cfg = write_toml(tmp_path / "c.toml", '[contacts]\n')
    with pytest.raises(imsgread.ConfigError):
        imsgread.write_contact("eric", imsgread.Contact('+1" , evil = "yes'), cfg)
    with pytest.raises(imsgread.ConfigError):
        imsgread.write_contact("not a name", imsgread.Contact("+1555"), cfg)


def test_a_write_that_would_corrupt_the_file_is_refused(tmp_path):
    """A [contacts.name] subtable reads fine but is invisible to a scan of the
    [contacts] table, so a naive insert would define the contact twice."""
    cfg = write_toml(tmp_path / "c.toml", (
        '[contacts]\nbrian = "+1555"\n\n[contacts.eric]\nid = "+1666"\nmarker = false\n'
    ))
    before = cfg.read_text()

    with pytest.raises(imsgread.ConfigError):
        imsgread.write_contact("eric", imsgread.Contact("+1666", True), cfg)

    assert cfg.read_text() == before
    assert imsgread.load_contact_entries(cfg)["eric"] == imsgread.Contact("+1666", False)


def test_config_mode_refuses_without_a_terminal(tmp_path, monkeypatch, capsys):
    """The mitigation is enforced, not documented: an agent cannot reach the
    code that would let it give itself a marker default."""
    from imsgread import main

    cfg = write_contacts(tmp_path / "c.toml", "eric", "+1555")
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(["--config", "--contacts-file", str(cfg)]) != 0
    assert "human at a terminal" in capsys.readouterr().err
    assert imsgread.load_contact_entries(cfg)["eric"].marker is None


@pytest.mark.parametrize("word, expected", [
    ("never", False), ("always", True), ("ask", None),
])
def test_config_mode_records_a_decision(tmp_path, monkeypatch, word, expected):
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = true }\n')
    replies = iter([f"set eric marker {word}", "quit"])
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))

    assert main(["--config", "--contacts-file", str(cfg)]) == 0
    assert imsgread.load_contact_entries(cfg)["eric"].marker is expected


def test_config_mode_adds_a_contact_and_keeps_its_decision(tmp_path, monkeypatch):
    """Repointing someone at a new number is not a reason to forget the
    decision already made about them."""
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = false }\n')
    replies = iter(["add mom +1777", "add eric +1888", "quit"])
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))

    assert main(["--config", "--contacts-file", str(cfg)]) == 0
    entries = imsgread.load_contact_entries(cfg)
    assert entries["mom"] == imsgread.Contact("+1777", None)
    assert entries["eric"] == imsgread.Contact("+1888", False)


def test_config_mode_survives_a_bad_command(tmp_path, monkeypatch, capsys):
    from imsgread import main

    cfg = write_contacts(tmp_path / "c.toml", "eric", "+1555")
    replies = iter(["nonsense", "set nobody marker never", "set eric marker sometimes", "quit"])
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: next(replies))

    assert main(["--config", "--contacts-file", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "unknown command" in out
    assert "unknown contact" in out
    assert "marker must be one of" in out


# --- finding groups ----------------------------------------------------------

def test_find_groups_lists_only_groups_the_contact_is_in(tmp_path, capsys, db):
    """A group id cannot be guessed, so something has to hand it over -- but
    scoped to one participant, never a dump of every conversation."""
    from imsgread import main

    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\nalice = "+15555550123"\n')
    code = main(["--find-groups", "--contact", "alice",
                 "--contacts-file", str(cfg), "--db", str(db)])
    out = capsys.readouterr().out

    assert code == 0
    assert "chat9001" in out
    assert "trail crew" in out
    assert "+15555559999" not in out       # the 1:1 chat is not a group
    assert "+15555550123" not in out       # and no numbers in the listing


def test_find_groups_refuses_without_a_participant(capsys, db):
    """No participant would mean enumerating every group on the machine."""
    from imsgread import main

    assert main(["--find-groups", "--db", str(db)]) != 0
    assert "error" in capsys.readouterr().err.lower()


def test_a_group_send_honors_the_alias_marker(tmp_path, messages, send_db, monkeypatch):
    """Per the decision recorded for this tool: a group alias carries one
    marker setting, exactly like a one-to-one contact."""
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\ncrew = { id = "chat9001", marker = true }\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    code = main(["--send", "--contact", "crew", "--message", "hi", "--confirm-timeout", "0",
                 "--contacts-file", str(cfg), "--db", str(send_db)])

    assert code == 0
    assert messages.calls == [("chat", "iMessage;+;chat9001", "hi \U0001F916")]


def test_a_one_to_one_send_prefers_the_chat_guid(tmp_path, messages, send_db, monkeypatch):
    """A green thread is only reachable through its own guid. The participant
    path is pinned to the iMessage account, so addressing an SMS or RCS chat
    that way writes the message into the thread and never delivers it."""
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+15555550123", marker = true }\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)

    assert main(send_args(cfg, send_db)) == 0
    assert messages.calls[-1][:2] == ("chat", "any;-;+15555550123")





# --------------------------------------------------------------------------
# delivery confirmation
# --------------------------------------------------------------------------

@pytest.fixture
def delivery_db(tmp_path: Path) -> Path:
    path = tmp_path / "delivery.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table message (ROWID integer primary key, is_from_me integer,
                              is_sent integer, is_delivered integer,
                              error integer, service text);
        create table chat (ROWID integer primary key, chat_identifier text);
        create table chat_message_join (chat_id integer, message_id integer);
        """
    )
    conn.execute("insert into chat values (1, '+15555550123')")
    conn.execute("insert into chat values (2, '+15555559999')")
    conn.commit()
    conn.close()
    return path


def add_sent(path: Path, rowid: int, chat: int, *, delivered=0, error=0, service="iMessage"):
    conn = sqlite3.connect(path)
    conn.execute("insert into message values (?,1,1,?,?,?)",
                 (rowid, delivered, error, service))
    conn.execute("insert into chat_message_join values (?,?)", (chat, rowid))
    conn.commit()
    conn.close()


def test_the_store_ignores_rows_at_or_below_the_floor(delivery_db):
    """The floor is what makes this the message we just sent rather than
    whatever already sat at the end of the thread."""
    add_sent(delivery_db, 10, 1, error=22)

    assert imsgread.ChatStore(delivery_db).sent_after("+15555550123", 10) is None


def test_the_store_never_reports_another_threads_row(delivery_db):
    """Scoped like every other query here: a failure in someone else's thread
    is not this send's failure."""
    add_sent(delivery_db, 10, 2, error=22)

    assert imsgread.ChatStore(delivery_db).sent_after("+15555550123", 0) is None


def test_the_floor_is_the_newest_outgoing_row(delivery_db):
    add_sent(delivery_db, 10, 1)
    add_sent(delivery_db, 11, 1)

    store = imsgread.ChatStore(delivery_db)
    assert store.last_sent_rowid("+15555550123") == 11
    assert store.last_sent_rowid("+15555559999") == 0


def test_a_failed_delivery_makes_the_command_fail(tmp_path, send_db, monkeypatch, capsys):
    """A send that never arrived must not exit 0. That was the bug."""
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = true }\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(imsgread, "TRANSPORT", FakeTransport(send_db, delivered=0, error=22))

    assert main(send_args(cfg, send_db)) == 1
    assert "NOT delivered over iMessage: error 22" in capsys.readouterr().err


def test_a_send_without_a_receipt_still_succeeds(tmp_path, send_db, monkeypatch, capsys):
    """Green threads routinely deliver without a receipt. The command says so
    rather than failing or claiming more than it knows."""
    cfg = write_toml(tmp_path / "c.toml",
                     '[contacts]\neric = { id = "+1555", marker = true }\n')
    monkeypatch.setattr("imsgread.sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(imsgread, "TRANSPORT", FakeTransport(send_db, delivered=0, service="SMS"))

    assert main(send_args(cfg, send_db)) == 0
    assert "no delivery receipt over SMS" in capsys.readouterr().err


def test_link_previews_are_not_reported_as_attachments(db):
    """Messages writes a pluginPayloadAttachment per URL. They cannot be
    opened or forwarded and the link is already in the message text, so
    listing them buries the real files under path noise."""
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    shot = next(m for m in msgs if m.has_attachment)

    assert len(shot.attachments) == 2
    assert "pluginPayload" not in shot.render()


# --------------------------------------------------------------------------
# staleness -- a quiet thread and a missing one look identical
# --------------------------------------------------------------------------

def test_a_thread_trailing_the_database_warns_on_stderr(db, capsys):
    """The fixture's messages are from 2023 and the file was written just now."""
    import json

    assert imsgread.main(["--chat", "+15555550123", "--db", str(db)]) == 0

    captured = capsys.readouterr()
    assert len(json.loads(captured.out)) == 3   # stdout stays parseable
    assert captured.err.startswith("note:")
    assert "not proof" in captured.err


def test_the_write_ahead_log_counts_as_the_database_being_updated(db, capsys):
    """New rows land in chat.db-wal first; the main file can lag it for hours."""
    import os

    newest_message = 978_307_200 + 700_000_000   # the fixture thread's last row, unix time
    os.utime(db, (newest_message + 60, newest_message + 60))
    (db.parent / "chat.db-wal").write_bytes(b"")   # mtime: now

    assert imsgread.main(["--chat", "+15555550123", "--db", str(db)]) == 0
    assert capsys.readouterr().err.startswith("note:")


NOW = datetime(2026, 11, 13, 0, 21, tzinfo=timezone.utc)


def _sent_at(iso: str) -> imsgread.Message:
    return imsgread.Message(date=iso, from_me=False, text="hi", has_attachment=False)


def test_a_database_nobody_has_written_to_in_days_is_reported():
    """The thread can be level with the database and both still be days old."""
    db_updated = datetime(2026, 11, 10, 18, 15, tzinfo=timezone.utc)
    note = imsgread.staleness_note(
        [_sent_at("2026-11-10T18:14:00+00:00")], db_updated, now=NOW
    )
    assert note == (
        "note: chat.db has not been updated since 2026-11-10 18:15 UTC; this "
        "Mac may not be receiving messages. Treat this thread as possibly "
        "incomplete."
    )


def test_an_empty_thread_is_not_reported_as_proof_of_silence():
    note = imsgread.staleness_note([], NOW, now=NOW)
    assert note == (
        "note: no messages found for this chat. That is not proof there are "
        "none -- check Messages on this Mac."
    )


@pytest.mark.parametrize("hours_behind, warns", [(23, False), (25, True)])
def test_a_thread_warns_only_once_it_trails_by_more_than_a_day(hours_behind, warns):
    from datetime import timedelta

    newest = NOW - timedelta(hours=hours_behind)
    note = imsgread.staleness_note([_sent_at(newest.isoformat())], NOW, now=NOW)
    assert (note is not None) == warns


# --------------------------------------------------------------------------
# chat store -- the one module that opens chat.db
# --------------------------------------------------------------------------

def test_the_store_reads_one_thread(db):
    msgs = imsgread.ChatStore(db).read_thread("+15555550123")
    assert {m.text for m in msgs} == {"plain text column wins", "first from them", None}


@pytest.mark.parametrize("make", [
    lambda p: None,                              # missing
    lambda p: p.write_bytes(b"not a database"),  # unreadable
])
def test_every_database_failure_is_one_store_error(tmp_path, make):
    """Read and send used to translate these three different ways."""
    path = tmp_path / "chat.db"
    make(path)
    with pytest.raises(imsgread.StoreError):
        imsgread.ChatStore(path).read_thread("+15555550123")


@pytest.mark.parametrize("chat_id, guid, is_group", [
    ("+15555550123", "iMessage;-;+15555550123", False),
    ("chat9001", "iMessage;+;chat9001", True),
    ("chat9002", None, True),             # a group Messages cannot address
    ("+15555550000", None, False),        # no row yet: a first message
])
def test_the_store_says_what_a_chat_is(db, chat_id, guid, is_group):
    chat = imsgread.ChatStore(db).chat(chat_id)
    assert (chat.guid, chat.is_group) == (guid, is_group)


def test_the_store_finds_only_groups_one_participant_is_in(db):
    groups = imsgread.ChatStore(db).find_groups("+15555559999")
    assert [g[0] for g in groups] == ["chat9001", "chat9002"]


def test_the_store_knows_when_it_was_last_written(db):
    import os

    os.utime(db, (1_700_000_000, 1_700_000_000))
    assert imsgread.ChatStore(db).updated().timestamp() == 1_700_000_000


# --------------------------------------------------------------------------
# send -- one call from text to a Delivery, behind a transport seam
# --------------------------------------------------------------------------

@pytest.fixture
def send_db(tmp_path: Path) -> Path:
    path = tmp_path / "send.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table message (ROWID integer primary key, is_from_me integer,
                              is_sent integer, is_delivered integer,
                              error integer, service text);
        create table chat (ROWID integer primary key, chat_identifier text,
                           guid text, display_name text);
        create table chat_message_join (chat_id integer, message_id integer);
        create table handle (ROWID integer primary key, id text);
        create table chat_handle_join (chat_id integer, handle_id integer);
        """
    )
    conn.executemany("insert into chat values (?,?,?,null)", [
        (1, "+15555550123", "any;-;+15555550123"),
        (2, "+15555559999", "iMessage;-;+15555559999"),
        (3, "chat9001", "iMessage;+;chat9001"),
        (4, "chat9002", None),
    ])
    conn.executemany("insert into handle values (?,?)",
                     [(1, "+15555550123"), (2, "+15555559999"), (3, "+15555551111")])
    conn.executemany("insert into chat_handle_join values (?,?)",
                     [(1, 1), (2, 2), (3, 1), (3, 2), (3, 3), (4, 1), (4, 2)])
    # an older failed send already at the end of the thread
    conn.execute("insert into message values (5, 1, 1, 0, 22, 'iMessage')")
    conn.execute("insert into chat_message_join values (1, 5)")
    conn.commit()
    conn.close()
    return path


class FakeTransport:
    """Stands in for Messages: records each send and writes the row Messages
    would write, so delivery confirmation runs for real against the db."""

    def __init__(self, db: Path, *, delivered=1, error=0, service="iMessage",
                 unaddressable=False):
        self.db, self.calls = db, []
        self.outcome = (delivered, error, service)
        self.unaddressable = unaddressable

    def to_chat(self, guid, body_path):
        self.calls.append(("chat", guid, Path(body_path).read_text(encoding="utf-8")))
        if self.unaddressable:
            raise imsgread.Unaddressable(f'Can’t get chat id "{guid}". (-1728)')
        self._write("guid", guid)

    def to_participant(self, handle, body_path):
        self.calls.append(("participant", handle, Path(body_path).read_text(encoding="utf-8")))
        self._write("chat_identifier", handle)

    def _write(self, column, value):
        conn = sqlite3.connect(self.db)
        row = conn.execute(f"select ROWID from chat where {column} = ?", (value,)).fetchone()
        chat = row[0] if row else conn.execute(
            "insert into chat (chat_identifier, guid) values (?, ?)",
            (value, f"any;-;{value}")).lastrowid
        delivered, error, service = self.outcome
        rowid = conn.execute(
            "insert into message (is_from_me, is_sent, is_delivered, error, service) "
            "values (1, 1, ?, ?, ?)", (delivered, error, service)).lastrowid
        conn.execute("insert into chat_message_join values (?, ?)", (chat, rowid))
        conn.commit()
        conn.close()


@pytest.fixture
def messages(send_db, monkeypatch) -> FakeTransport:
    """The command line's transport, swapped for the fake."""
    fake = FakeTransport(send_db)
    monkeypatch.setattr(imsgread, "TRANSPORT", fake)
    return fake


def test_a_send_is_addressed_by_guid_marked_and_confirmed(send_db):
    fake = FakeTransport(send_db)
    delivery = imsgread.send(imsgread.ChatStore(send_db), fake, "+15555550123", "hi",
                             robot=True, timeout=0)

    assert fake.calls == [("chat", "any;-;+15555550123", "hi \U0001F916")]
    assert delivery.render() == "delivered over iMessage"


def test_an_undelivered_send_reports_the_error_code(send_db):
    """The failure this path exists for: Messages accepted the text, osascript
    exited 0, and the send failed afterwards with error 22."""
    fake = FakeTransport(send_db, delivered=0, error=22)
    delivery = imsgread.send(imsgread.ChatStore(send_db), fake, "+15555550123", "hi",
                             robot=False, timeout=0)

    assert (delivery.status, delivery.error) == ("failed", 22)
    assert delivery.render() == "NOT delivered over iMessage: error 22"


def test_a_missing_receipt_is_not_called_a_failure(send_db):
    """Plain SMS returns no receipt at all. Reading a missing flag as failure
    would cry wolf on every green thread that worked perfectly well."""
    fake = FakeTransport(send_db, delivered=0, service="SMS")
    delivery = imsgread.send(imsgread.ChatStore(send_db), fake, "+15555550123", "hi",
                             robot=False, timeout=0)

    assert delivery.render() == "no delivery receipt over SMS"


def test_a_first_message_goes_to_the_participant(send_db):
    """Nobody has a chat row before the first message. That is the one case
    where addressing a participant directly is the only option."""
    fake = FakeTransport(send_db)
    imsgread.send(imsgread.ChatStore(send_db), fake, "+15555550000", "hi",
                  robot=False, timeout=0)

    assert fake.calls == [("participant", "+15555550000", "hi")]


def test_a_send_with_an_unreadable_database_sends_nothing(tmp_path, send_db):
    """Without the database there is no way to tell a group from a person, or
    to confirm anything afterwards -- so nothing goes out."""
    fake = FakeTransport(send_db)
    with pytest.raises(imsgread.StoreError):
        imsgread.send(imsgread.ChatStore(tmp_path / "missing.db"), fake, "+15555550123", "hi",
                      robot=False, timeout=0)
    assert fake.calls == []


def test_an_unaddressable_one_to_one_chat_falls_back_to_the_participant(send_db):
    """chat.db keeps rows Messages will not address -- a chat with yourself,
    or a thread Messages has pruned -- and they fail with -1728. Reaching the
    person over iMessage beats not delivering at all."""
    fake = FakeTransport(send_db, unaddressable=True)
    imsgread.send(imsgread.ChatStore(send_db), fake, "+15555550123", "hi",
                  robot=False, timeout=0)

    assert [c[:2] for c in fake.calls] == [
        ("chat", "any;-;+15555550123"), ("participant", "+15555550123")]


def test_an_unaddressable_group_is_refused_not_sent_to_one_person(send_db):
    """The fallback is for one-to-one chats only. Taking it for a group would
    hand the group's identifier to the single-participant script."""
    fake = FakeTransport(send_db, unaddressable=True)
    with pytest.raises(imsgread.Unaddressable, match="group"):
        imsgread.send(imsgread.ChatStore(send_db), fake, "chat9001", "hi",
                      robot=False, timeout=0)

    assert [c[0] for c in fake.calls] == ["chat"]


def test_a_group_with_no_guid_is_refused_before_sending(send_db):
    """No guid means the group cannot be addressed. Falling back to
    `participant` would send to one member instead of the group."""
    fake = FakeTransport(send_db)
    with pytest.raises(imsgread.ScopeError, match="group"):
        imsgread.send(imsgread.ChatStore(send_db), fake, "chat9002", "hi",
                      robot=False, timeout=0)
    assert fake.calls == []


@pytest.mark.parametrize("chat_id, text, error", [
    ("", "hi", imsgread.ScopeError),
    ("   ", "hi", imsgread.ScopeError),
    (None, "hi", imsgread.ScopeError),
    ("+15555550123", "   ", ValueError),
])
def test_a_send_with_no_target_or_no_body_sends_nothing(send_db, chat_id, text, error):
    fake = FakeTransport(send_db)
    with pytest.raises(error):
        imsgread.send(imsgread.ChatStore(send_db), fake, chat_id, text,
                      robot=False, timeout=0)
    assert fake.calls == []



# --- the Messages adapter: the one place osascript runs -----------------------

def fake_osascript(monkeypatch, *, returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(list(cmd))
        class R:
            pass
        R.returncode, R.stderr = returncode, stderr
        return R()

    monkeypatch.setattr("imsgread.subprocess.run", run)
    return calls


def test_the_adapter_sends_to_a_participant_with_the_body_as_a_file(monkeypatch, tmp_path):
    """The body goes by file: inline quoting mangles apostrophes and emoji."""
    calls = fake_osascript(monkeypatch)
    body = tmp_path / "b.txt"
    imsgread.MessagesTransport().to_participant("+15555550123", str(body))

    script = calls[0][2]
    assert "participant" in script and "chat id" not in script
    assert calls[0][3:] == [str(body), "+15555550123"]


def test_the_adapter_addresses_a_chat_by_its_guid(monkeypatch, tmp_path):
    """A guid carries the service prefix; that is how SMS, RCS and group
    threads are reached."""
    calls = fake_osascript(monkeypatch)
    imsgread.MessagesTransport().to_chat("iMessage;+;chat9001", str(tmp_path / "b.txt"))

    assert "chat id" in calls[0][2]
    assert calls[0][-1] == "iMessage;+;chat9001"


def test_the_adapter_reports_minus_1728_as_unaddressable(monkeypatch, tmp_path):
    fake_osascript(monkeypatch, returncode=1,
                   stderr='Can\u2019t get chat id "any;-;+1555". (-1728)')
    with pytest.raises(imsgread.Unaddressable):
        imsgread.MessagesTransport().to_chat("any;-;+1555", str(tmp_path / "b.txt"))


def test_any_other_applescript_failure_is_a_plain_error(monkeypatch, tmp_path):
    """Only -1728 is a routing problem. Everything else is a real error and
    must not be retried into a different delivery path."""
    fake_osascript(monkeypatch, returncode=1,
                   stderr="Messages got an error: something else (-1712)")
    with pytest.raises(RuntimeError) as exc:
        imsgread.MessagesTransport().to_chat("any;-;+1555", str(tmp_path / "b.txt"))
    assert not isinstance(exc.value, imsgread.Unaddressable)
