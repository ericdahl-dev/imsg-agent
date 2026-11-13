# 1. A contact's stored marker setting answers for agents too

Date: 2026-11-13
Status: Accepted

## Context

Each contact in `contacts.toml` may carry a standing marker decision:
`marker = true` (always append 🤖), `marker = false` (never), or nothing (ask).

`--send` decides whether to mark in this order: `--robot`/`--no-robot`, then
the contact's stored setting, then a human at a terminal, otherwise refuse.

A code review (PR #13) argued that a stored "never" should not answer for a
program, because an agent could edit `contacts.toml` and give itself
permission to send unmarked. PR #14 implemented that: a "never" contact
needed an explicit `--no-robot` from a non-interactive caller.

## Decision

Rejected. The configured setting is followed for agents and humans alike.
A stored `marker = false` lets an agent send unmarked without `--no-robot`,
and a stored `marker = true` marks without `--robot`.

> "We should still follow the configured robot/no robot settings."

PR #14 was closed unmerged.

## Consequences

- The setting is a decision a person made ahead of time about that contact;
  requiring a flag on top of it would make the setting meaningless for the
  agents it exists for.
- The protection against an agent granting itself "never" stays where it is:
  `--config` refuses to run without a terminal. An agent that writes
  `contacts.toml` directly is outside what this tool can enforce.
- Future reviews should not re-propose asymmetric handling of "never" or
  requiring `--no-robot` for contacts configured not to be marked.
