# imsg-agent

Read a single iMessage thread from `chat.db` — decoded and scoped — and send
to one, marked when an agent wrote it.

Built to replace a block of instructions that had been copy-pasted into two
different `AGENTS.md` files. Two copies of a privacy rule is where privacy
rules start drifting.

```bash
imsg-agent --contact friend -n 20 --text
imsg-agent --chat +15555550123 -n 50 > thread.json
imsg-agent --send --contact friend --message-file reply.txt --robot
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
the exact leak `contacts.toml` exists to prevent. `--send --contact friend` does
not. Sending is scoped identically to reading: one identifier, no broadcast
path, no "reply to whoever was last".

**Agent-sent messages say so.** `--robot` appends 🤖 to the body. It is
provenance, not decoration: someone reading the thread later can see which
messages a person actually typed. Marking is never chosen by default for a
program — a non-interactive caller that passes neither `--robot` nor
`--no-robot` is refused, because silence from an agent is not consent to send
unmarked. A human at a terminal is simply asked.

**One marker decision per contact, made once.** In practice the right choice is
the same for a given person every time, so a contact can carry a standing one:

```toml
[contacts]
brian = "+15555550123"                              # asked per message
eric  = { id = "+15555550124", marker = false }     # never marked
```

An explicit flag still wins; a contact with no setting behaves exactly as
before. This is the one place a value may answer for a program that said
nothing, which is a real weakening of the rule above and only sound while a
person is the one who wrote it. So the setting is not editable by a program:
`imsg-agent --config` refuses to run without a terminal. A config file an agent
can write is not a record of consent, it is a note the agent left itself.

**The marker goes in the body, because a tapback cannot be trusted.** A 🤖
reaction would read better than an appended emoji, and it is not supported on
purpose. Messages' entire AppleScript dictionary is `login`, `logout`, `send` —
reactions are not scriptable, so the only route is UI scripting through System
Events. On macOS 26 the Messages accessibility tree exposes the transcript as
nested `AXGroup`s with no addressable message bubbles, so there is nothing to
attach a reaction to. Worse, the failure is quiet: an unattached tapback leaves
a message the caller believes is marked and the recipient sees unmarked, which
is the exact thing the marker exists to prevent. An appended emoji either goes
out or the send fails.

**Sending never steals focus.** The AppleScript uses `launch`, not `activate`,
so Messages starts in the background if it is not already running and stays
behind whatever you are doing.

**It's cheaper.** An agent following prose instructions re-derives the SQL and
re-implements the decoder on every run, then retries when the decode comes back
empty. This is one call returning structured rows.

## Install

Python 3.11+ and no runtime dependencies. macOS only — it reads Apple's
`chat.db` and sends through Messages.

```bash
git clone https://github.com/ericdahl-dev/imsg-agent && cd imsg-agent
pip install -e '.[dev]'    # installs the `imsg-agent` command, plus pytest
python3 -m pytest -q       # 38 tests, no network, no real message data
```

`pip install -e .` alone is enough if you don't want to run the tests; pytest
is the only dev dependency. The editable install is the recommended one — it
keeps the checkout authoritative, so `git pull` is the upgrade.

Then configure a contact:

```bash
mkdir -p ~/.config/imsg-agent
cp contacts.toml.example ~/.config/imsg-agent/contacts.toml   # then fill in
imsg-agent --contacts                                          # confirm
```

Contacts are searched for in this order, first hit wins:

| Location | Use |
| --- | --- |
| `$IMSG_AGENT_CONTACTS` | explicit override for a shell or a session |
| `~/.config/imsg-agent/contacts.toml` | normal install; survives upgrades |
| `contacts.toml` beside the module | a clone or editable install, i.e. the repo root |

`--contacts-file PATH` skips the search entirely and reads that file.

The repo-root copy is gitignored, so a checkout never commits real identifiers.
Named contacts keep phone numbers out of shell history, CI logs, and agent
transcripts — callers pass `--contact friend`, not a raw number.

**Full Disk Access is required.** Grant it to your terminal under
System Settings → Privacy & Security → Full Disk Access, or every read fails
with a permission error. Granting it does not reach a process that is already
running — restart the terminal afterwards.

## Install the Claude skill

`.claude/skills/imsg-agent/SKILL.md` teaches an agent the safe usage: always
scoped, `--message-file` over shell quoting, resolve a name to a contact rather
than guessing, and never unmarked by default. Inside this repo Claude Code
picks it up automatically. To use it from your other projects, link it into
your personal skills directory:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/.claude/skills/imsg-agent" ~/.claude/skills/imsg-agent
```

Link rather than copy, deliberately. A copy is a second place the safety rules
live, and it goes stale the first time the ones here change — the drift this
repo exists to end. A symlink makes `git pull` the update.

No slash command is needed: the skill's `description` is what makes an agent
reach for it when you say "text Eric" or "what did she say yesterday". A new
session picks up a changed description; an already-running one does not.

Its commands call the installed `imsg-agent` executable rather than a path into
this repo, so they work from whatever directory the agent happens to be in.

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
--contacts-file PATH alternate contacts.toml for this run

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

Or as a module, if you have a clone but no install: `python3 imsgread.py
--contact friend -n 20 --text`.

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
