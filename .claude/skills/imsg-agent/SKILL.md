---
name: imsg-agent
description: Safely read and send scoped iMessage threads with imsg-agent instead of direct chat.db or AppleScript access.
---

# imsg-agent

Use this skill when you need to read or send iMessages from Claude Code on a Mac where this repo is available.

`imsg-agent` is the supported path for iMessage work. Do not query `~/Library/Messages/chat.db` directly, do not reimplement the typedstream decoder, and do not write ad-hoc AppleScript that embeds phone numbers in commands or transcripts.

## Safety rules

- Always scope every read or send to one `--contact` or one `--chat` identifier.
- Prefer `--contact CONTACT_NAME` from `contacts.toml` so phone numbers stay out of shell history, CI logs, and agent transcripts. `CONTACT_NAME` is a configured contact alias, not a literal name to copy.
- Never read across all chats. This tool has no unscoped mode; do not try to bypass that with SQL.
- Never send to “the last conversation” or any inferred recipient. Resolve the target explicitly.
- The marker reflects whose words they are, not who typed them. For agent-authored sends, use `--robot` so the outgoing message is marked with 🤖.
- Use `--no-robot` only for words a human wrote or approved as their own, and only when they approved that specific message. Drafting in someone's voice and having them say "send it" is theirs; composing on their behalf unreviewed is yours. Never omit the marker choice in non-interactive runs — an agent must not be able to send unmarked by default.
- Prefer `--message-file` for anything longer than a short plain-text message, or anything with quotes, apostrophes, newlines, or emoji.
- If reading fails on macOS, check Full Disk Access for the terminal before changing code. Granting it does not reach a process that is already running: restart the session afterwards, or every read still fails.
- Timestamps are printed in UTC. `16:44` is `12:44` US Eastern — do not read a message as hours newer than it is.

## Common commands

List configured contacts:

```bash
python3 imsgread.py --contacts
```

Read the latest messages for one contact as human-readable text:

```bash
python3 imsgread.py --contact CONTACT_NAME -n 20 --text
```

Read the latest messages for one explicit chat identifier as JSON:

```bash
python3 imsgread.py --chat +155****0123 -n 50
```

Read oldest-first when you need conversation flow:

```bash
python3 imsgread.py --contact CONTACT_NAME -n 50 --oldest-first --text
```

Draft a message in a file, then send it marked as agent-authored:

```bash
printf '%s\n' 'On my way.' > reply.txt
python3 imsgread.py --send --contact CONTACT_NAME --message-file reply.txt --robot
```

Send unmarked only when the user explicitly asks for that:

```bash
python3 imsgread.py --send --contact CONTACT_NAME --message-file reply.txt --no-robot
```

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
python3 imsgread.py --send --contact CONTACT_NAME --message 'it works'
```

The first is unscoped: it reads across every conversation on the machine. The second leaks a phone number into command history. The third puts the body through shell quoting, where apostrophes and emoji are mangled and the text lands in history — and it leaves the marker unstated, so it is unsafe for a non-interactive agent on two counts.
