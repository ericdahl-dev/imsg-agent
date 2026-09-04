"""Read one iMessage thread from chat.db, and send to one, decoded and scoped.

Two things make this worth being code instead of instructions in an AGENTS.md:

1. Most message bodies are not in `message.text`. They live in `attributedBody`
   as a serialized NSAttributedString (an NSArchiver "typedstream"). A query
   that reads only `text` comes back mostly blank and looks like an empty
   conversation. The decoder here handles that.

2. Scoping is a safety property, not a preference. `~/Library/Messages/chat.db`
   holds every conversation on the machine. This module cannot be asked for
   "all messages" -- every read path requires a chat identifier. A rule written
   in prose depends on the reader choosing to follow it; this one does not.

The database is opened with `mode=ro` and never written. Sending goes through
AppleScript, never through chat.db, and is scoped the same way reads are: one
identifier, no broadcast path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from tempfile import NamedTemporaryFile
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# contacts.toml is looked for in several places so the tool works the same
# whether it was cloned or installed. Installing into site-packages leaves
# nowhere sensible to keep a config file, and an upgrade would wipe it.
CONTACTS_ENV = "IMSG_AGENT_CONTACTS"
CONTACTS_FILENAME = "contacts.toml"
CONFIG_CONTACTS = Path(
    os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
) / "imsg-agent" / CONTACTS_FILENAME
# Alongside the module: the repo root for a clone or an editable install.
BUNDLED_CONTACTS = Path(__file__).with_name(CONTACTS_FILENAME)

# macOS stores message.date as nanoseconds since 2001-01-01 UTC (10.13+).
# Much older rows use seconds. Anything past this bound is nanoseconds.
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
NANOSECOND_BOUND = 10**11


class ScopeError(ValueError):
    """Raised when a read is attempted without a chat identifier."""


# --------------------------------------------------------------------------
# typedstream decoding
# --------------------------------------------------------------------------

def _read_length(buf: bytes, i: int) -> tuple[int, int]:
    """Read a typedstream integer at `i`. Returns (value, next_index).

    Encoding: a byte below 0x81 is the literal value; 0x81 introduces a
    little-endian uint16; 0x82 introduces a little-endian uint32.
    """
    marker = buf[i]
    if marker == 0x81:
        return int.from_bytes(buf[i + 1 : i + 3], "little"), i + 3
    if marker == 0x82:
        return int.from_bytes(buf[i + 1 : i + 5], "little"), i + 5
    return marker, i + 1


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the message text from an archived NSAttributedString.

    The payload looks like:

        \\x04\\x0bstreamtyped ... NSString\\x01\\x94\\x84\\x01+\\x0eyea XF is good\\x86 ...

    The first NSString after the header is the message body; the ones that
    follow belong to the attribute dictionary. `+` is the C-string type marker,
    followed by a length and then the bytes themselves.
    """
    if not blob:
        return None
    start = blob.find(b"NSString")
    if start < 0:
        return None
    marker = blob.find(b"+", start)
    if marker < 0:
        return None
    try:
        length, body = _read_length(blob, marker + 1)
    except IndexError:
        return None
    if length <= 0 or body + length > len(blob):
        return None
    return blob[body : body + length].decode("utf-8", errors="replace")


def apple_time(raw: int | None) -> datetime | None:
    """Convert a chat.db timestamp to an aware UTC datetime."""
    if not raw:
        return None
    seconds = raw / 1e9 if raw > NANOSECOND_BOUND else float(raw)
    return APPLE_EPOCH + timedelta(seconds=seconds)


# --------------------------------------------------------------------------
# contacts
# --------------------------------------------------------------------------

def contacts_candidates() -> list[Path]:
    """Every path searched for contacts.toml, in precedence order.

    An explicit environment variable wins, then the user's config directory,
    then a copy sitting next to the module -- which is the repo root for a
    clone or an editable install, and how this tool behaved before it was
    installable.
    """
    paths: list[Path] = []
    env = os.environ.get(CONTACTS_ENV)
    if env:
        paths.append(Path(env).expanduser())
    paths.append(CONFIG_CONTACTS)
    paths.append(BUNDLED_CONTACTS)
    return paths


def contacts_path() -> Path:
    """The contacts file in effect, or where to create one if none exists."""
    for candidate in contacts_candidates():
        if candidate.exists():
            return candidate
    return CONFIG_CONTACTS


def load_contacts(path: Path | None = None) -> dict[str, str]:
    """Map friendly names to chat identifiers, so callers pass `friend` rather
    than a phone number. Keeps raw numbers out of shell history and logs."""
    if path is None:
        path = contacts_path()
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: str(v) for k, v in data.get("contacts", {}).items()}


def resolve_chat(
    chat: str | None,
    contact: str | None,
    contacts_file: Path | None = None,
) -> str:
    """Return a chat identifier, or raise. There is deliberately no default."""
    if chat:
        return chat
    if contact:
        contacts = load_contacts(contacts_file)
        if contact not in contacts:
            known = ", ".join(sorted(contacts)) or "none configured"
            raise ScopeError(f"unknown contact {contact!r} (known: {known})")
        return contacts[contact]
    raise ScopeError(
        "a chat identifier is required -- pass --chat or --contact. "
        "This tool has no unscoped mode."
    )


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

@dataclass
class Message:
    date: str | None
    from_me: bool
    text: str | None
    has_attachment: bool

    def render(self) -> str:
        stamp = (self.date or "")[:16].replace("T", " ")
        who = "me " if self.from_me else "them"
        body = self.text or ""
        if self.has_attachment:
            body = (body + " [attachment]").strip()
        return f"{stamp}  {who}  {body}"


QUERY = """
select m.date, m.is_from_me, m.text, m.attributedBody, m.cache_has_attachments
from message m
join chat_message_join cmj on cmj.message_id = m.ROWID
join chat c on c.ROWID = cmj.chat_id
where c.chat_identifier = ?
order by m.date desc
limit ?
"""


@contextmanager
def _connect(db_path: Path = DEFAULT_DB):
    """Open chat.db read-only. The database is never written by this tool."""
    if not db_path.exists():
        raise FileNotFoundError(f"no chat database at {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:  # pragma: no cover - env specific
        raise PermissionError(
            f"cannot open {db_path}: {exc}. Grant Full Disk Access to your "
            "terminal in System Settings > Privacy & Security."
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def read_thread(
    chat_id: str,
    limit: int = 50,
    db_path: Path = DEFAULT_DB,
    include_empty: bool = False,
) -> list[Message]:
    """Read the most recent messages for one chat identifier, newest first.

    `chat_id` is required and is always bound as a parameter -- there is no
    code path that reads across threads.
    """
    if not chat_id:
        raise ScopeError("chat_id is required")

    with _connect(db_path) as conn:
        rows = conn.execute(QUERY, (chat_id, limit)).fetchall()

    out: list[Message] = []
    for raw_date, is_from_me, text, blob, has_attachment in rows:
        body = text if text else decode_attributed_body(blob)
        if not include_empty and not (body and body.strip()) and not has_attachment:
            continue
        stamp = apple_time(raw_date)
        out.append(
            Message(
                date=stamp.isoformat() if stamp else None,
                from_me=bool(is_from_me),
                text=body,
                has_attachment=bool(has_attachment),
            )
        )
    return out


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

AGENT_MARKER = "\U0001F916"  # robot face


def mark_agent_sent(text: str, *, robot: bool) -> str:
    """Append the agent marker so a recipient can tell who actually wrote this.

    Provenance, not decoration: a person reading the thread months later should
    be able to see which messages a human typed. Appended rather than prefixed
    so the message still opens with its own first line.
    """
    if not robot:
        return text
    if text.rstrip().endswith(AGENT_MARKER):
        return text
    return f"{text.rstrip()} {AGENT_MARKER}"


# `launch` rather than `activate`: it starts Messages in the background if it is
# not already running. `activate`, and letting `tell` cold-start the app, pulls
# Messages in front of whatever the user is doing.
SEND_SCRIPT = """on run argv
set t to (read (POSIX file (item 1 of argv)) as \u00abclass utf8\u00bb)
launch application "Messages"
tell application "Messages" to send t to participant (item 2 of argv) of (first account whose service type is iMessage)
end run"""


def send_message(chat: str | None, text: str, *, robot: bool) -> None:
    """Send one iMessage to one chat identifier.

    Scoped the same way reads are: no identifier, no send. There is no
    broadcast path and no "reply to whoever was last" convenience.

    The body goes via a UTF-8 file rather than inline in the AppleScript.
    Inline quoting mangles apostrophes and emoji, and the agent marker is an
    emoji, so this is not optional.
    """
    identifier = (chat or "").strip()
    if not identifier:
        raise ScopeError("a chat identifier is required to send")

    body = mark_agent_sent(text, robot=robot)
    if not body.strip():
        raise ValueError("refusing to send an empty message")

    with NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as handle:
        handle.write(body)
        body_path = handle.name

    try:
        result = subprocess.run(
            ["osascript", "-e", SEND_SCRIPT, body_path, identifier],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"osascript failed: {result.stderr.strip()}")
    finally:
        os.unlink(body_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imsg-agent",
        description="Read one iMessage thread from chat.db, decoded and scoped.",
        epilog="A chat identifier is always required; there is no unscoped mode.",
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument("--chat", help="chat identifier, e.g. +15555550123")
    target.add_argument("--contact", help="name from contacts.toml, e.g. friend")
    p.add_argument("-n", "--limit", type=int, default=50, help="messages to read (default 50)")
    p.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    p.add_argument("--oldest-first", action="store_true", help="reverse to chronological order")
    p.add_argument("--include-empty", action="store_true", help="keep rows with no body")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to chat.db")
    p.add_argument("--contacts", action="store_true", help="list configured contacts and exit")
    p.add_argument("--contacts-file", type=Path, default=None,
                   help=f"path to contacts.toml (default: ${CONTACTS_ENV}, then {CONFIG_CONTACTS})")
    p.add_argument("--send", action="store_true", help="send a message instead of reading")
    body = p.add_mutually_exclusive_group()
    body.add_argument("--message", help="message body (prefer --message-file for anything with quotes or emoji)")
    body.add_argument("--message-file", type=Path, help="file containing the message body, read as UTF-8")
    marker = p.add_mutually_exclusive_group()
    marker.add_argument("--robot", dest="robot", action="store_true", default=None,
                        help=f"append {AGENT_MARKER} so the recipient can see an agent sent it")
    marker.add_argument("--no-robot", dest="robot", action="store_false",
                        help="send without the agent marker")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _resolve_marker_choice(robot: bool | None) -> bool | None:
    """Decide whether to mark, asking a human but never guessing for a program.

    A non-interactive caller that says nothing gets refused rather than
    defaulted. Silence from an agent is not consent to send unmarked.
    """
    if robot is not None:
        return robot
    if not sys.stdin.isatty():
        print(
            "error: pass --robot or --no-robot when not running interactively",
            file=sys.stderr,
        )
        return None
    answer = input(f"Mark this as sent by an agent ({AGENT_MARKER})? [Y/n] ").strip().lower()
    return answer not in {"n", "no"}


def _run_send(args: argparse.Namespace) -> int:
    if args.message is None and args.message_file is None:
        print("error: --send needs --message or --message-file", file=sys.stderr)
        return 1

    try:
        chat_id = resolve_chat(args.chat, args.contact, args.contacts_file)
        text = (
            args.message_file.read_text(encoding="utf-8")
            if args.message_file is not None
            else args.message
        )
    except (ScopeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    robot = _resolve_marker_choice(args.robot)
    if robot is None:
        return 1

    try:
        send_message(chat_id, text, robot=robot)
    except (ScopeError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"sent to {chat_id}{' ' + AGENT_MARKER if robot else ''}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.contacts:
        contacts = load_contacts(args.contacts_file)
        if not contacts:
            searched = "\n  ".join(str(c) for c in contacts_candidates())
            print(
                f"no contacts configured -- create {args.contacts_file or CONFIG_CONTACTS}\n"
                f"searched:\n  {searched}",
                file=sys.stderr,
            )
            return 1
        for name, ident in sorted(contacts.items()):
            print(f"{name}\t{ident}")
        return 0

    if args.send:
        return _run_send(args)

    try:
        chat_id = resolve_chat(args.chat, args.contact, args.contacts_file)
        messages = read_thread(
            chat_id,
            limit=args.limit,
            db_path=args.db,
            include_empty=args.include_empty,
        )
    except (ScopeError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.oldest_first:
        messages.reverse()

    if args.text:
        for m in messages:
            print(m.render())
    else:
        json.dump([asdict(m) for m in messages], sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
