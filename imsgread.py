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


class ConfigError(ValueError):
    """Raised when contacts.toml cannot be understood.

    Separate from ScopeError because the fix is different: a scope error means
    the caller must say who they mean, a config error means a file on disk is
    wrong and every command will keep failing until it is edited.
    """


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


@dataclass(frozen=True)
class Contact:
    """One entry from contacts.toml.

    `marker` records a standing decision about the agent marker for this
    person: True always marks, False never does, and None means no decision
    was recorded, which is the behaviour this tool has always had -- ask a
    human, refuse a program.
    """

    identifier: str
    marker: bool | None = None


# The config vocabulary. `ask` is the absence of a decision rather than a third
# state, so it is stored by leaving the key out entirely.
MARKER_WORDS: dict[str, bool | None] = {"always": True, "never": False, "ask": None}


def marker_word(marker: bool | None) -> str:
    """The word a person types, for a stored value."""
    for word, value in MARKER_WORDS.items():
        if value is marker:
            return word
    raise ValueError(f"not a marker value: {marker!r}")


def _parse_contact(name: str, value: object) -> Contact:
    """Accept either form: a bare identifier, or a table carrying settings.

    Unknown keys are an error rather than ignored. A silently inert `marker`
    setting is the worst outcome here: the caller believes a standing decision
    is in force and every send quietly does the opposite.
    """
    if isinstance(value, str):
        return Contact(value)
    if not isinstance(value, dict):
        raise ConfigError(
            f"contact {name!r} must be an identifier or a table, got {type(value).__name__}"
        )
    unknown = sorted(set(value) - {"id", "marker"})
    if unknown:
        raise ConfigError(
            f"contact {name!r} has unknown key(s): {', '.join(unknown)} "
            f"(expected 'id' and optionally 'marker')"
        )
    identifier = value.get("id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConfigError(f"contact {name!r} is a table but has no 'id'")
    marker = value.get("marker")
    if marker is not None and not isinstance(marker, bool):
        raise ConfigError(
            f"contact {name!r} has a non-boolean 'marker' "
            f"(use true, false, or leave it out to be asked)"
        )
    return Contact(identifier, marker)


def load_contact_entries(path: Path | None = None) -> dict[str, Contact]:
    """Every contact with its settings."""
    if path is None:
        path = contacts_path()
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return {k: _parse_contact(k, v) for k, v in data.get("contacts", {}).items()}


def load_contacts(path: Path | None = None) -> dict[str, str]:
    """Map friendly names to chat identifiers, so callers pass `friend` rather
    than a phone number. Keeps raw numbers out of shell history and logs."""
    return {k: c.identifier for k, c in load_contact_entries(path).items()}


def contact_marker(
    contact: str | None,
    contacts_file: Path | None = None,
) -> bool | None:
    """The standing marker decision for a contact, or None if there is none.

    None for `--chat` too: a raw identifier is not a contact, so there is
    nowhere for a decision to have been recorded and the caller is asked or
    refused exactly as before.
    """
    if not contact:
        return None
    return load_contact_entries(contacts_file).get(contact, Contact("")).marker


CONTACTS_HEADER = """# imsg-agent contacts. Named contacts keep phone numbers out of shell history,
# CI logs, and agent transcripts: callers pass `--contact friend`.
#
# A bare identifier leaves the agent marker to be chosen per message. A table
# with `marker = true` always appends it and `marker = false` never does --
# a standing decision, so write it yourself rather than letting an agent.

[contacts]
"""


def _valid_contact_name(name: str) -> bool:
    """A TOML bare key, so the file we write back is still parseable."""
    return bool(name) and all(c.isalnum() or c in "_-" for c in name)


def format_contact_line(name: str, contact: Contact) -> str:
    """One TOML line for a contact.

    A contact with no standing decision keeps the plain form it has always
    had, so turning the feature on for one person does not rewrite the shape
    of everyone else's entry.
    """
    if contact.marker is None:
        return f'{name} = "{contact.identifier}"'
    flag = "true" if contact.marker else "false"
    return f'{name} = {{ id = "{contact.identifier}", marker = {flag} }}'


def _verified(text: str, name: str, contact: Contact, path: Path) -> str:
    """Never hand back a config a person has to repair by hand.

    The line editor below understands the shapes this tool writes. A file
    someone wrote themselves can hold others -- a `[contacts.name]` subtable
    reads fine but is invisible to a scan of the `[contacts]` table, so
    inserting a line there would define the same contact twice and every later
    command would fail on a file nobody knowingly broke. Parsing the result
    first turns that into a refusal.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"that edit would leave {path} unparseable ({exc}); it is unchanged"
        ) from exc
    written = {k: _parse_contact(k, v) for k, v in data.get("contacts", {}).items()}
    if written.get(name) != contact:
        raise ConfigError(
            f"could not set {name!r} in {path}; it is unchanged -- edit the file by hand"
        )
    return text


def write_contact(name: str, contact: Contact, path: Path | None = None) -> Path:
    """Insert or replace one contact, leaving the rest of the file alone.

    Rewriting the file from parsed data would drop the comments a person put in
    their own config, so this edits only the line that changes.
    """
    if not _valid_contact_name(name):
        raise ConfigError(
            f"contact name {name!r} must be letters, digits, dashes or underscores"
        )
    identifier = contact.identifier.strip()
    if not identifier:
        raise ConfigError("a contact needs an identifier")
    if any(c in identifier for c in '"\\\n\r'):
        raise ConfigError(f"identifier {identifier!r} contains a character that cannot be written")
    contact = Contact(identifier, contact.marker)

    if path is None:
        path = contacts_path()
    line = format_contact_line(name, contact)

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _verified(CONTACTS_HEADER + line + "\n", name, contact, path), encoding="utf-8"
        )
        return path

    lines = path.read_text(encoding="utf-8").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "[contacts]"), None)
    if start is None:
        body = "\n".join(lines).rstrip("\n")
        prefix = body + "\n\n" if body else ""
        path.write_text(
            _verified(f"{prefix}[contacts]\n{line}\n", name, contact, path), encoding="utf-8"
        )
        return path

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break

    for i in range(start + 1, end):
        # Only the key half, so a commented-out entry is never mistaken for one.
        if lines[i].split("=", 1)[0].strip() == name:
            lines[i] = line
            break
    else:
        insert = end
        while insert > start + 1 and not lines[insert - 1].strip():
            insert -= 1
        lines.insert(insert, line)

    path.write_text(
        _verified("\n".join(lines) + "\n", name, contact, path), encoding="utf-8"
    )
    return path


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

    Trailing whitespace goes first, on every path. A --message-file body is a
    file, and files end in a newline, so leaving it would send a blank line to
    the recipient on the strength of how the draft was saved.
    """
    text = text.rstrip()
    if not robot:
        return text
    if text.endswith(AGENT_MARKER):
        return text
    return f"{text} {AGENT_MARKER}"


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
    p.add_argument("--config", action="store_true",
                   help="edit contacts.toml interactively (requires a terminal)")
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


def _resolve_marker_choice(
    robot: bool | None,
    configured: bool | None = None,
) -> bool | None:
    """Decide whether to mark, asking a human but never guessing for a program.

    Most specific wins: an explicit --robot/--no-robot, then the contact's
    standing decision from contacts.toml, then a human at a terminal, and
    otherwise a refusal.

    Silence from an agent is still not consent to send unmarked -- that is what
    the last branch is for. A configured value is not silence: it is a decision
    a person made about this contact ahead of time, which is why it may answer
    for a program when nothing else can. That only holds while a person is the
    one who wrote it, so `--config` refuses to run without a terminal.
    """
    if robot is not None:
        return robot
    if configured is not None:
        return configured
    if not sys.stdin.isatty():
        print(
            "error: pass --robot or --no-robot when not running interactively",
            file=sys.stderr,
        )
        return None
    answer = input(f"Mark this as sent by an agent ({AGENT_MARKER})? [Y/n] ").strip().lower()
    return answer not in {"n", "no"}


CONFIG_COMMANDS = """commands:
  list                         contacts and their marker setting
  add NAME IDENTIFIER          add a contact, or point an existing one elsewhere
  set NAME marker WORD         standing agent-marker decision: always, never, ask
  quit"""


def _print_config(path: Path, entries: dict[str, Contact]) -> None:
    print(f"contacts.toml: {path}")
    if not entries:
        print("  (none configured)")
        return
    width = max(len(name) for name in entries)
    for name, contact in sorted(entries.items()):
        print(f"  {name:<{width}}  {contact.identifier}  marker: {marker_word(contact.marker)}")


def _run_config(args: argparse.Namespace) -> int:
    """Edit contacts.toml, but only for a person sitting at a terminal.

    A contact's marker setting is allowed to answer for a program that passed
    no flag, which is only sound while a person is the one who put it there.
    Refusing without a tty is what makes that true: an agent cannot give itself
    permission to send unmarked, because it cannot reach this code at all.
    """
    if not sys.stdin.isatty():
        print(
            "error: config changes must be made by a human at a terminal",
            file=sys.stderr,
        )
        return 1

    path = args.contacts_file or contacts_path()
    try:
        entries = load_contact_entries(path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_config(path, entries)
    print()
    print(CONFIG_COMMANDS)

    while True:
        try:
            parts = input("> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not parts:
            continue
        cmd, rest = parts[0].lower(), parts[1:]

        if cmd in {"quit", "exit", "q"}:
            return 0
        if cmd in {"help", "?"}:
            print(CONFIG_COMMANDS)
            continue

        try:
            if cmd == "list":
                entries = load_contact_entries(path)
                _print_config(path, entries)
            elif cmd == "add":
                if len(rest) != 2:
                    print("usage: add NAME IDENTIFIER")
                    continue
                name, identifier = rest
                existing = entries.get(name)
                # Repointing someone at a new number is not a reason to forget
                # the marker decision already made about them.
                keep = existing.marker if existing else None
                write_contact(name, Contact(identifier, keep), path)
                entries = load_contact_entries(path)
                print(f"{name} -> {identifier}  marker: {marker_word(keep)}")
            elif cmd == "set":
                if len(rest) != 3 or rest[1].lower() != "marker":
                    print("usage: set NAME marker always|never|ask")
                    continue
                name, word = rest[0], rest[2].lower()
                if name not in entries:
                    print(f"unknown contact {name!r} -- add it first")
                    continue
                if word not in MARKER_WORDS:
                    print(f"marker must be one of: {', '.join(MARKER_WORDS)}")
                    continue
                write_contact(name, Contact(entries[name].identifier, MARKER_WORDS[word]), path)
                entries = load_contact_entries(path)
                print(f"{name} marker: {word}")
            else:
                print(f"unknown command {cmd!r}")
                print(CONFIG_COMMANDS)
        except (ConfigError, OSError) as exc:
            print(f"error: {exc}")


def _run_send(args: argparse.Namespace) -> int:
    if args.message is None and args.message_file is None:
        print("error: --send needs --message or --message-file", file=sys.stderr)
        return 1

    try:
        chat_id = resolve_chat(args.chat, args.contact, args.contacts_file)
        configured = contact_marker(args.contact, args.contacts_file)
        text = (
            args.message_file.read_text(encoding="utf-8")
            if args.message_file is not None
            else args.message
        )
    except (ScopeError, ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    robot = _resolve_marker_choice(args.robot, configured)
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

    if args.config:
        return _run_config(args)

    if args.contacts:
        try:
            entries = load_contact_entries(args.contacts_file)
        except ConfigError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if not entries:
            searched = "\n  ".join(str(c) for c in contacts_candidates())
            print(
                f"no contacts configured -- create {args.contacts_file or CONFIG_CONTACTS}\n"
                f"searched:\n  {searched}",
                file=sys.stderr,
            )
            return 1
        for name, contact in sorted(entries.items()):
            # Only shown when set, so a contact with no standing decision reads
            # exactly as it did before settings existed.
            suffix = "" if contact.marker is None else f"\tmarker={marker_word(contact.marker)}"
            print(f"{name}\t{contact.identifier}{suffix}")
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
    except (ScopeError, ConfigError, FileNotFoundError, PermissionError) as exc:
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
