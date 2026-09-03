# imsg-agent

Read a single iMessage thread from `chat.db` — decoded and scoped — and send
to one, marked when an agent wrote it.

Built to replace a block of instructions that had been copy-pasted into two
different `AGENTS.md` files. Two copies of a privacy rule is where privacy
rules start drifting.

```bash
python3 imsgread.py --contact disco -n 20 --text
python3 imsgread.py --chat +15555550123 -n 50 > thread.json
python3 imsgread.py --send --contact disco --message-file reply.txt --robot
```

## Why this is code

**Most message bodies are not in `message.text`.** They live in
`attributedBody` as a serialized `NSAttributedString` — an NSArchiver
"typedstream" blob. A query that reads only `text` comes back mostly blank and
looks like an empty conversation. On a 400-message sample of a real thread,
**zero** rows had a usable `text` column and 398 needed decoding; the two that
didn't decode had no body at all (one attachment-only, one empty).

**Scoping is a safety property, not a preference.** `~/Library/Messages/chat.db`
holds every conversation on the machine — family, friends, personal logistics.
There is no code path here that reads across threads: `read_thread` requires a
chat identifier, and the CLI exits non-zero without one. A rule written in prose
depends on the reader choosing to follow it. This one does not.

**Sending belongs here too, for the same reason reads do.** The alternative was
a block of `osascript` copy-pasted into an `AGENTS.md`, with the phone number
inline — which put the number into shell history and every agent transcript,
the exact leak `contacts.toml` exists to prevent. `--send --contact disco` does
not. Sending is scoped identically to reading: one identifier, no broadcast
path, no "reply to whoever was last".

**Agent-sent messages say so.** `--robot` appends 🤖 to the body. It is
provenance, not decoration: someone reading the thread later can see which
messages a person actually typed. Marking is never chosen by default for a
program — a non-interactive caller that passes neither `--robot` nor
`--no-robot` is refused, because silence from an agent is not consent to send
unmarked. A human at a terminal is simply asked.

**It's cheaper.** An agent following prose instructions re-derives the SQL and
re-implements the decoder on every run, then retries when the decode comes back
empty. This is one call returning structured rows.

## Install

Python 3.11+, no dependencies.

```bash
cp contacts.toml.example contacts.toml   # then fill in
python3 -m pytest -q                      # 21 tests, no network, no real data
```

`contacts.toml` is gitignored. Named contacts keep phone numbers out of shell
history, CI logs, and agent transcripts — callers pass `--contact disco`, not a
raw number.

**Full Disk Access is required.** Grant it to your terminal under
System Settings → Privacy & Security → Full Disk Access, or every read fails
with a permission error.

## Usage

```
--chat ID            chat identifier, e.g. +15555550123
--contact NAME       name from contacts.toml
-n, --limit N        messages to read (default 50)
--text               human-readable output instead of JSON
--oldest-first       chronological instead of newest-first
--include-empty      keep rows with no body
--db PATH            alternate chat.db
--contacts           list configured contacts and exit

--send               send instead of reading
--message TEXT       message body
--message-file PATH  body from a UTF-8 file (prefer this: inline quoting
                     mangles apostrophes and emoji)
--robot              append 🤖 so the recipient can see an agent sent it
--no-robot           send unmarked
```

One of `--chat` or `--contact` is always required.

JSON output, newest first:

```json
[
  {
    "date": "2026-08-19T04:15:09.239040+00:00",
    "from_me": false,
    "text": "yea XF is good",
    "has_attachment": false
  }
]
```

`has_attachment` is worth reading. A message can carry an image and no text at
all, which for a thread where someone sends scans is signal, not noise.

## As a library

```python
from imsgread import read_thread

for m in read_thread("+15555550123", limit=20):
    print(m.date, "me" if m.from_me else "them", m.text)
```

```python
from imsgread import send_message

send_message("+15555550123", "on my way", robot=True)   # sends "on my way 🤖"
```

`read_thread` raises `ScopeError` on an empty identifier, `FileNotFoundError`
if the database is missing, and `PermissionError` with a pointer to the Full
Disk Access setting when macOS blocks the read.

## The blob format

```
\x04\x0bstreamtyped ... NSString\x01\x94\x84\x01+\x0eyea XF is good\x86 ...
                                              ^^^ ^^ ^
                                              |   |  the bytes
                                              |   length (14)
                                              C-string type marker
```

Lengths are varints: a byte below `0x81` is the literal value, `0x81`
introduces a little-endian uint16, `0x82` a uint32. The first `NSString` after
the header is the message body — the ones after it belong to the attribute
dictionary, so the decoder deliberately stops at the first.

## Tests

Every fixture blob is synthesised by `make_blob` in the shape macOS writes, so
no real message content is committed. Coverage includes the single-byte/uint16
length boundary, multibyte characters, truncated buffers, the
attribute-string trap, legacy second-precision timestamps, and — most
importantly — that a read against one thread never returns rows from another.

Sending is covered without sending anything: `subprocess.run` is patched, so the
tests assert the marked body and the identifier that *would* have been handed to
AppleScript. They also pin the refusals — empty identifier, empty body, and a
non-interactive caller that did not state a marker choice.
