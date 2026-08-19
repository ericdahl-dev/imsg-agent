"""Read a single iMessage thread from chat.db, decoded and scoped.

Two things make this worth being code instead of instructions in an AGENTS.md:

1. Most message bodies are not in `message.text`. They live in `attributedBody`
   as a serialized NSAttributedString (an NSArchiver "typedstream"). A query
   that reads only `text` comes back mostly blank and looks like an empty
   conversation. The decoder here handles that.

2. Scoping is a safety property, not a preference. `~/Library/Messages/chat.db`
   holds every conversation on the machine. This module cannot be asked for
   "all messages" -- every read path requires a chat identifier. A rule written
   in prose depends on the reader choosing to follow it; this one does not.

Read-only throughout. The database is opened with `mode=ro` and never written.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tomllib
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

__version__ = "0.1.0"

DEFAULT_DB = Path.home() / "Library" / "Messages" / "chat.db"
CONTACTS_FILE = Path(__file__).with_name("contacts.toml")

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

def load_contacts(path: Path = CONTACTS_FILE) -> dict[str, str]:
    """Map friendly names to chat identifiers, so callers pass `disco` rather
    than a phone number. Keeps raw numbers out of shell history and logs."""
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return {k: str(v) for k, v in data.get("contacts", {}).items()}


def resolve_chat(chat: str | None, contact: str | None) -> str:
    """Return a chat identifier, or raise. There is deliberately no default."""
    if chat:
        return chat
    if contact:
        contacts = load_contacts()
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
    if not db_path.exists():
        raise FileNotFoundError(f"no chat database at {db_path}")

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:  # pragma: no cover - env specific
        raise PermissionError(
            f"cannot open {db_path}: {exc}. Grant Full Disk Access to your "
            "terminal in System Settings > Privacy & Security."
        ) from exc

    try:
        rows = conn.execute(QUERY, (chat_id, limit)).fetchall()
    finally:
        conn.close()

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

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imsg-read",
        description="Read one iMessage thread from chat.db, decoded and scoped.",
        epilog="A chat identifier is always required; there is no unscoped mode.",
    )
    target = p.add_mutually_exclusive_group()
    target.add_argument("--chat", help="chat identifier, e.g. +15555550123")
    target.add_argument("--contact", help="name from contacts.toml, e.g. disco")
    p.add_argument("-n", "--limit", type=int, default=50, help="messages to read (default 50)")
    p.add_argument("--text", action="store_true", help="human-readable output instead of JSON")
    p.add_argument("--oldest-first", action="store_true", help="reverse to chronological order")
    p.add_argument("--include-empty", action="store_true", help="keep rows with no body")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="path to chat.db")
    p.add_argument("--contacts", action="store_true", help="list configured contacts and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.contacts:
        contacts = load_contacts()
        if not contacts:
            print(f"no contacts configured -- create {CONTACTS_FILE}", file=sys.stderr)
            return 1
        for name, ident in sorted(contacts.items()):
            print(f"{name}\t{ident}")
        return 0

    try:
        chat_id = resolve_chat(args.chat, args.contact)
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
