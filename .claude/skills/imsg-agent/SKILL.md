---
name: imsg-agent
description: Read or send iMessages and texts on macOS. Use when the user wants to text someone, send or reply to an iMessage, or check what someone said in a thread. Always scoped to one contact; never reads chat.db directly.
---

# imsg-agent

Use this skill when you need to read or send iMessages from Claude Code on a Mac where imsg-agent is installed.

The commands below call the installed `imsg-agent` executable, so they work from any working directory. If the shell reports `command not found: imsg-agent`, it is not installed yet -- run `pip install -e .` (or `uv tool install -e .`, or `pipx install -e .`) inside the imsg-agent checkout, then retry. Do not substitute a path to `imsgread.py` guessed from the current directory.

`imsg-agent` is the supported path for iMessage work. Do not query `~/Library/Messages/chat.db` directly, do not reimplement the typedstream decoder, and do not write ad-hoc AppleScript that embeds phone numbers in commands or transcripts.

## Safety rules

- Always scope every read or send to one `--contact` or one `--chat` identifier.
- A person's name is not a chat identifier. When the user names someone ("text Eric", "message my sister"), run `imsg-agent --contacts` and use the matching alias. If there is no clear match, ask which contact they mean -- never guess which identifier a name refers to, and never fall back to a number seen earlier in the conversation. Sending to the wrong person is not recoverable: there is no unsend after two minutes, and no delete that reaches the recipient.
- Prefer `--contact CONTACT_NAME` from `contacts.toml` so phone numbers stay out of shell history, CI logs, and agent transcripts. `CONTACT_NAME` is a configured contact alias, not a literal name to copy.
- Never read across all chats. This tool has no unscoped mode; do not try to bypass that with SQL.
- Never send to “the last conversation” or any inferred recipient. Resolve the target explicitly.
- An attachment cannot carry the marker. A file has no body to append 🤖 to, so
  `--file` sends it unmarked even under `--robot`; only an accompanying message is
  marked. Send a file on its own only when the recipient knowing an agent chose it
  does not matter, and pair it with a marked line when it does.
- The marker reflects whose words they are, not who typed them. For agent-authored sends, use `--robot` so the outgoing message is marked with 🤖.
- Use `--no-robot` only for words a human wrote or approved as their own, and only when they approved that specific message. Drafting in someone's voice and having them say "send it" is theirs; composing on their behalf unreviewed is yours.
- A contact may carry a standing decision in `contacts.toml` (`marker = true` always marks, `marker = false` never does), which answers when no flag is passed. Where there is no such setting, omitting the choice in a non-interactive run is still refused -- an agent must not be able to send unmarked by default.
- Never write that setting yourself. It is the record of a decision a person made, and it is the only thing allowed to answer for a program, so an agent that sets it has granted itself permission to send unmarked. `imsg-agent --config` refuses to run without a terminal for exactly this reason. Ask the user to run it rather than writing the key with a shell redirect or an editor tool. Adding or repointing a contact alias is fine -- an identifier decides who a message reaches, which the user has already told you, and a wrong one fails loudly. A marker default decides what the recipient is told about who wrote it, silently, on every later send.
- The marker is appended to the message body. Tapbacks are deliberately unsupported: Messages has no reaction API, and UI scripting cannot confirm one attached, so a tapback marker can silently fail to appear. Do not try to add one with AppleScript or System Events.
- Prefer `--message-file` for anything longer than a short plain-text message, or anything with quotes, apostrophes, newlines, or emoji.
- If reading fails on macOS, check Full Disk Access for the terminal before changing code. Granting it does not reach a process that is already running: restart the session afterwards, or every read still fails.
- Timestamps are printed in UTC. `16:44` is `12:44` US Eastern — do not read a message as hours newer than it is.

## Common commands

Contacts live in `$IMSG_AGENT_CONTACTS`, then `~/.config/imsg-agent/contacts.toml`, then a `contacts.toml` beside the module. List configured contacts:

```bash
imsg-agent --contacts
```

Read the latest messages for one contact as human-readable text:

```bash
imsg-agent --contact CONTACT_NAME -n 20 --text
```

Read the latest messages for one explicit chat identifier as JSON:

```bash
imsg-agent --chat +155****0123 -n 50
```

Read oldest-first when you need conversation flow:

```bash
imsg-agent --contact CONTACT_NAME -n 50 --oldest-first --text
```

Draft a message in a file, then send it marked as agent-authored:

```bash
printf '%s\n' 'On my way.' > reply.txt
imsg-agent --send --contact CONTACT_NAME --message-file reply.txt --robot
```

Send unmarked only when the user explicitly asks for that:

```bash
imsg-agent --send --contact CONTACT_NAME --message-file reply.txt --no-robot
```

A contact's standing decision, if it has one, shows in the contact listing and
answers when no flag is given. Only the user can change it, at a terminal:

```bash
imsg-agent --config
```

## Group chats

A group identifier cannot be guessed. Ask which groups a contact is already in:

```bash
imsg-agent --find-groups --contact CONTACT_NAME
```

That prints one line per group -- identifier, size, and name where the group has
one. There is deliberately no way to list every group on the machine; the answer
is always scoped to one participant you already have configured.

Read one the same way as any other thread, using the identifier it printed:

```bash
imsg-agent --chat chat123456789012345678 -n 30 --text
```

A group read attributes each message to its sender: a configured contact by name,
anyone else by a masked handle like `*1387`. A one-to-one read is unchanged and
still shows `them`, which is what keeps the other person's number out of the
transcript. Add a group to `contacts.toml` like any other contact if you read it
often -- a group alias carries one standing marker decision covering everyone in it.

## Attachments

Reads report what came with a message, not just that something did:

```
2026-08-27 22:05  them  [image/png 196.3 KB ~/Library/Messages/Attachments/70/00/.../Screenshot.png]
```

Messages prunes old files, so an attachment that is no longer on disk reports
`missing` rather than failing. Link previews are not reported at all: they are
payload files that cannot be opened or forwarded, and the link is already in the
message text.

Send a file with `--file`, on its own or with a message:

```bash
imsg-agent --send --contact CONTACT_NAME --file ~/Pictures/shot.png --robot
```

Read the marker rule above before sending a file: the file itself goes unmarked.

## Expected workflow

1. Identify the exact contact or chat identifier from the user’s request or `contacts.toml`.
2. Read only that thread with `--contact` or `--chat`.
3. Summarize or draft from the scoped output.
4. Before sending, show the exact message and target unless the user already gave both explicitly.
5. Send with `--robot` for agent-authored text, or `--no-robot` only when requested.

## Do not do this

```bash
sqlite3 ~/Library/Messages/chat.db 'select * from message limit 100;'
osascript -e 'tell application "Messages" to send "hi" to buddy "+155****0123"'
imsg-agent --send --contact CONTACT_NAME --message 'it works'
```

The first is unscoped: it reads across every conversation on the machine. The second leaks a phone number into command history. The third puts the body through shell quoting, where apostrophes and emoji are mangled and the text lands in history — and it leaves the marker unstated, so it is unsafe for a non-interactive agent on two counts.
