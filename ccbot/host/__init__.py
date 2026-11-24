"""The session host: a long-lived process that owns the Claude Agent SDK clients.

A `claude` child dies with the process that started it (verified 2026-11-24),
so sessions kept inside the bot would die on every bot restart. The host is
the process that does not restart: it holds one `ClaudeSDKClient` per session
and talks to the bot over a Unix socket (`protocol.py`). It is deliberately
thin — raw data out, commands in — so that everything that changes often
(cards, wording, routing) lives in the bot, which can be restarted freely.
"""
