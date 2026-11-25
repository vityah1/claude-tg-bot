"""One live Claude session inside the host: a task that owns its SDK client.

The SDK's client has to be entered and left in the same task (anyio cancel
scopes), so each session is an actor: `_run` opens the client, a reader task
turns what Claude sends into events, and every call on the client — a prompt,
an interrupt, a mode switch — goes through the actor's queue and is made by
that one task. One session failing, hanging or being slow touches nobody
else: its exceptions end here, as a `fault` event and a dormant session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ConversationResetMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    PermissionUpdate,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import SystemPromptPreset

if TYPE_CHECKING:
    from .server import Host

log = logging.getLogger("ccbot.host.actor")

# A start that takes longer than this is a start that failed (the CLI hangs
# on something nobody can see from here).
START_TIMEOUT = 120.0
# Tool input travels to the bot for the card and the "🔧" line; a Write of a
# whole file has no business in the journal in full.
_INPUT_LIMIT = 20_000
_TEXT_LIMIT = 60_000
_STDERR_KEEP = 30


def claude_bin() -> str | None:
    """The system `claude`, so the CLI on disk (and its updates) is the one run.

    The same candidates as the bot's `sessions.claude_bin`, checked on every
    start rather than cached: a stale CCBOT_CLAUDE_BIN (an nvm version since
    removed) must not break a launch while a working binary is on PATH.
    None falls back to the copy bundled with the SDK.
    """
    candidates: list[Path] = []
    raw = (os.getenv("CCBOT_CLAUDE_BIN") or "").strip()
    if raw:
        candidates.append(Path(raw).expanduser())
    found = shutil.which("claude")
    if found:
        candidates.append(Path(found))
    candidates += [Path.home() / ".local" / "bin" / "claude",
                   Path("/usr/local/bin/claude"), Path("/usr/bin/claude")]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def _cut(value: Any, limit: int) -> Any:
    """Shorten long strings inside a tool input, keeping its shape."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… [+{len(value) - limit}]"
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def _suggestion(s: PermissionUpdate) -> dict[str, Any]:
    try:
        return s.to_dict()
    except Exception:            # an update the SDK cannot serialise: show its type
        return {"type": getattr(s, "type", "?")}


CLAUDE_CODE_PROMPT: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}


class StartError(Exception):
    pass


@dataclass
class Prompt:
    """A prompt sent to the CLI and not yet answered by a result.

    The CLI does not hold a prompt sent mid-turn until the turn ends: it takes
    it into the running turn once the current tool call finishes. Until then
    (`taken` False) it can still be taken back (`SessionActor.cancel`).
    """
    uuid: str
    text: str
    taken: bool = False


def encode_inflight(prompts: list[Prompt]) -> str | None:
    return json.dumps([{"u": p.uuid, "t": p.text, "k": p.taken} for p in prompts]) \
        if prompts else None


def decode_inflight(raw: str | None) -> list[Prompt]:
    """The prompts a host left unanswered; rows of an older build are dropped.

    Builds before per-prompt uuids wrote a plain list of texts, and they
    never cleared a prompt merged into another's turn, so such a row cannot
    be told from a stale one: reporting it would announce long-answered
    prompts as cut off.
    """
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [Prompt(str(r.get("u") or ""), str(r.get("t") or ""), bool(r.get("k")))
            for r in rows if isinstance(r, dict)]


async def _one(uid: str, text: str) -> AsyncIterator[dict[str, Any]]:
    """A prompt as the dict the CLI takes, carrying our uuid."""
    yield {"type": "user", "uuid": uid, "parent_tool_use_id": None,
           "message": {"role": "user", "content": text}}


async def _cancel_async(client: ClaudeSDKClient, uid: str) -> dict[str, Any]:
    """Take a prompt back from the CLI's queue (`cancel_async_message`).

    The SDK has no public call for it: this is its private control channel,
    usable because the SDK version is pinned — re-check it on an upgrade.
    """
    query = getattr(client, "_query", None)
    send = getattr(query, "_send_control_request", None)
    if send is None:
        raise RuntimeError("this SDK cannot take a prompt back")
    return await send({"subtype": "cancel_async_message", "message_uuid": uid}, 15.0)


class SessionActor:
    def __init__(self, host: Host, name: str, sid: str, cwd: str):
        self.host = host
        self.name = name
        self.sid = sid
        self.cwd = cwd
        self.state = "starting"       # starting | idle | busy | waiting | dormant | closed
        self.prompts: list[Prompt] = []  # sent and not answered by a result yet
        # Prompts answered lately: taking one back is "too late", not "unknown".
        self.answered: deque[str] = deque(maxlen=200)
        self.requests: dict[str, tuple[asyncio.Future, ToolPermissionContext]] = {}
        self.calls: asyncio.Queue[tuple[Callable[[ClaudeSDKClient], Awaitable[Any]],
                                        asyncio.Future] | None] = asyncio.Queue()
        self.ready = asyncio.Event()
        self.start_error: str | None = None
        self.task: asyncio.Task | None = None
        # Asked to leave (`stopping`), and whether that is the session's end
        # (`ending`) or only this process letting go of it (a host shutdown,
        # a restart onto a new CLI): the latter leaves it dormant, resumable.
        self.stopping = False
        self.ending = False
        self.reset_pending = False
        self.tasks: set[asyncio.Task] = set()
        self.stderr: deque[str] = deque(maxlen=_STDERR_KEEP)
        self.info: dict[str, Any] = {}
        self.limits: dict[str, Any] = {}
        self.last_event = time.monotonic()
        self._end_reason = ""
        self.effort: str | None = None

    # -- lifecycle -----------------------------------------------------
    async def start(self, resume: bool, model: str | None = None,
                    mode: str | None = None, effort: str | None = None) -> None:
        self.effort = effort
        self.task = asyncio.create_task(self._run(resume, model, mode),
                                        name=f"actor:{self.name}")
        try:
            await asyncio.wait_for(self.ready.wait(), START_TIMEOUT)
        except TimeoutError:
            self.start_error = self.start_error or "the session did not start in time"
        if self.start_error:
            await self.close()
            raise StartError(self.start_error)

    def _options(self, resume: bool, model: str | None,
                 mode: str | None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=self.cwd,
            cli_path=claude_bin(),
            resume=self.sid if resume else None,
            session_id=None if resume else self.sid,
            # The launch name, exactly as `claude -n` gives it: history shows
            # it, and it is the key the bot finds a session by. The replay
            # echoes each prompt back at the moment a turn takes it in.
            extra_args={"name": self.name, "replay-user-messages": None},
            # Claude Code's own system prompt, as the terminal has it. Left
            # unset, the SDK passes `--system-prompt ""`: every SDK session ran
            # without it from the move on 2026-11-24 (found the same evening).
            system_prompt=CLAUDE_CODE_PROMPT,
            model=model,
            effort=self.effort,  # pyright: ignore[reportArgumentType]
            # A mode picked from the chat outlives a wake-up; None leaves the
            # one from the user's settings.
            permission_mode=mode,  # pyright: ignore[reportArgumentType]
            can_use_tool=self._can_use_tool,
            stderr=self._on_stderr,
        )

    def _on_stderr(self, line: str) -> None:
        self.stderr.append(line.rstrip())
        log.debug("[%s] stderr: %s", self.name, line.rstrip())

    async def _run(self, resume: bool, model: str | None, mode: str | None) -> None:
        reason = ""
        try:
            async with ClaudeSDKClient(self._options(resume, model, mode)) as client:
                with contextlib.suppress(Exception):
                    server = await client.get_server_info() or {}
                    self.info.update({
                        "version": server.get("claude_code_version"),
                        "mode": server.get("current_permission_mode"),
                        "account": (server.get("account") or {}).get("email"),
                    })
                self.state = "idle"
                self.ready.set()
                self.host.emit(self.name, "session_up",
                               {"sid": self.sid, "cwd": self.cwd, "resumed": resume,
                                "info": self.info})
                reader = asyncio.create_task(self._read(client), name=f"reader:{self.name}")
                try:
                    while True:
                        item = await self.calls.get()
                        if item is None:
                            break
                        fn, fut = item
                        try:
                            result = await fn(client)
                        except Exception as exc:
                            if not fut.done():
                                fut.set_exception(exc)
                        else:
                            if not fut.done():
                                fut.set_result(result)
                finally:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reader
                    reason = self._end_reason
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("[%s] session failed", self.name)
            reason = f"{type(exc).__name__}: {exc}"
            if not self.ready.is_set():
                self.start_error = self._explain(reason)
                self.ready.set()
        finally:
            self._lose_requests()
            self._drain_calls()
            ended = "closed" if self.ending else "dormant"
            self.state = ended
            if self.ready.is_set() and not self.start_error:
                if not self.stopping and not self.host.shutting_down:
                    self.host.emit(self.name, "fault",
                                   {"kind": "session_ended", "message": self._explain(reason),
                                    "prompts": [p.text for p in self.prompts]})
                self.host.actor_ended(self, ended)

    def _explain(self, reason: str) -> str:
        tail = " | ".join(line for line in self.stderr if line)[-600:]
        return (reason or "the Claude process exited") + (f" — {tail}" if tail else "")

    async def close(self, end: bool = True) -> None:
        """Leave the client in its own task and wait; *end* False leaves it dormant."""
        self.stopping = True
        self.ending = end
        self.calls.put_nowait(None)
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 30)
            except (TimeoutError, asyncio.CancelledError):
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self.task
            except Exception:
                log.debug("[%s] close: task ended with an error", self.name, exc_info=True)

    def _drain_calls(self) -> None:
        while not self.calls.empty():
            item = self.calls.get_nowait()
            if item is not None and not item[1].done():
                item[1].set_exception(RuntimeError("session ended"))

    def _lose_requests(self) -> None:
        for rid, (fut, _ctx) in list(self.requests.items()):
            if not fut.done():
                fut.cancel()
            if self.host.store.close_request(rid, "lost", None):
                self.host.emit(self.name, "request_closed", {"rid": rid, "how": "lost"})
        self.requests.clear()

    # -- calls on the client --------------------------------------------
    async def call(self, fn: Callable[[ClaudeSDKClient], Awaitable[Any]],
                   timeout: float = 60.0) -> Any:
        if self.state in ("dormant", "closed") or self.stopping:
            raise RuntimeError("session is not running")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((fn, fut))
        return await asyncio.wait_for(fut, timeout)

    @property
    def asking(self) -> bool:
        return bool(self.requests)

    def _update_state(self) -> None:
        if self.state in ("dormant", "closed", "starting"):
            return
        new = "waiting" if self.requests else ("busy" if self.prompts else "idle")
        if new != self.state:
            self.state = new
            self.host.emit(self.name, "session_state", {"state": new}, journal=False)

    def _persist(self) -> None:
        self.host.store.set_session(self.name, inflight=encode_inflight(self.prompts))

    async def send(self, text: str) -> tuple[Prompt, bool]:
        """A prompt, and whether it has to wait: the session was working."""
        waits = self.state in ("busy", "waiting") or bool(self.prompts)
        prompt = Prompt(uuid.uuid4().hex, text)
        # The intent is written down before the call: a host that dies in the
        # middle of the turn can still say which prompt it was.
        self.prompts.append(prompt)
        self._persist()
        self._update_state()
        try:
            await self.call(lambda c: c.query(_one(prompt.uuid, text)))
        except Exception:
            if prompt in self.prompts:
                self.prompts.remove(prompt)
            self._persist()
            self._update_state()
            raise
        self.host.emit(self.name, "turn_start", {"text": text[:500]})
        return prompt, waits

    def _prompt(self, uid: str | None) -> Prompt | None:
        return next((p for p in self.prompts if uid and p.uuid == uid), None)

    def _mark_taken(self, uid: str | None) -> None:
        """A turn took the prompt in: from now on it is past taking back."""
        prompt = self._prompt(uid)
        if prompt is None or prompt.taken:
            return
        prompt.taken = True
        self._persist()
        self.host.emit(self.name, "prompt_taken", {"uuid": prompt.uuid}, journal=False)

    async def cancel(self, uid: str) -> dict[str, Any]:
        """Take back a prompt no turn has taken in yet."""
        prompt = self._prompt(uid)
        if prompt is None:
            return {"cancelled": False, "why": "taken" if uid in self.answered else "unknown"}
        if prompt.taken:
            return {"cancelled": False, "why": "taken"}
        res = await self.call(lambda c: _cancel_async(c, uid), timeout=20)
        if not res.get("cancelled"):
            self._mark_taken(uid)
            return {"cancelled": False, "why": "taken"}
        if prompt in self.prompts:
            self.prompts.remove(prompt)
        self._persist()
        self._update_state()
        log.info("[%s] prompt %s taken back", self.name, uid[:8])
        return {"cancelled": True}

    def _close_prompts(self, msg: ResultMessage) -> None:
        """Drop the prompts this result answered.

        A result names the prompts its turn took in; those, and every prompt
        already seen taken, are done. A result naming none of ours ends a turn
        the session started itself (a background task's notice: its origin
        says so, and its names are empty when it took in nothing of ours).
        Only a result with no names that no injected turn produced — an older
        CLI — falls back to the oldest prompt.
        """
        ids = set(msg.user_message_uuids or [])
        if msg.user_message_uuid:
            ids.add(msg.user_message_uuid)
        done = [p for p in self.prompts if p.taken or p.uuid in ids]
        kind = (msg.origin or {}).get("kind")
        if not done and not ids and self.prompts and kind in (None, "human"):
            done = self.prompts[:1]
        self.prompts = [p for p in self.prompts if p not in done]
        self.answered.extend(p.uuid for p in done)

    # -- what Claude sends ---------------------------------------------
    async def _read(self, client: ClaudeSDKClient) -> None:
        try:
            async for msg in client.receive_messages():
                self.last_event = time.monotonic()
                try:
                    self._handle(msg)
                except Exception:
                    log.exception("[%s] could not handle %s", self.name, type(msg).__name__)
            self._end_reason = "the Claude process exited"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[%s] reader ended: %s", self.name, exc)
            self._end_reason = f"{type(exc).__name__}: {exc}"
        if not self.stopping:
            # The process is gone: let the command loop leave the client too.
            self.calls.put_nowait(None)

    def _follow_sid(self, sid: str | None, strong: bool) -> None:
        """/clear gives the conversation a new id on the same process."""
        if not sid or sid == self.sid:
            return
        if not (self.reset_pending or strong):
            return
        old, self.sid, self.reset_pending = self.sid, sid, False
        log.info("[%s] session id %s -> %s", self.name, old[:8], sid[:8])
        self.host.store.set_session(self.name, sid=sid)
        self.host.emit(self.name, "session_reset", {"old_sid": old, "new_sid": sid})

    def _handle(self, msg: Any) -> None:
        if isinstance(msg, ConversationResetMessage):
            self.reset_pending = True
            return
        if isinstance(msg, SystemMessage):
            data = msg.data or {}
            self._follow_sid(data.get("session_id"), strong=msg.subtype == "init")
            if msg.subtype == "init":
                self.info.update({k: data.get(k) for k in
                                  ("model", "claude_code_version") if k in data})
                if "permissionMode" in data:
                    self.info["mode"] = data["permissionMode"]
                if "claude_code_version" in data:
                    self.info["version"] = data["claude_code_version"]
                self.host.store.set_session(self.name, info=self.info)
            elif msg.subtype == "status":
                self.host.emit(self.name, "activity",
                               {"status": data.get("status"),
                                "compact_result": data.get("compact_result")}, journal=False)
            elif msg.subtype == "compact_boundary":
                self.host.emit(self.name, "notice", {"kind": "compacted",
                                                    "meta": data.get("compact_metadata")})
            elif msg.subtype == "background_tasks_changed":
                self.host.emit(self.name, "background", {"tasks": data.get("tasks") or []})
            return
        if isinstance(msg, UserMessage):
            # The replay of a prompt at the moment a turn takes it in; tool
            # results carry uuids of their own, never one of ours.
            if not msg.parent_tool_use_id:
                self._mark_taken(msg.uuid)
            return
        if isinstance(msg, AssistantMessage):
            self._follow_sid(msg.session_id, strong=False)
            for uid in msg.user_message_uuids or []:
                self._mark_taken(uid)
            if msg.parent_tool_use_id:
                return                  # a subagent's own words stay with it
            if msg.model:
                self.info["model"] = msg.model
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self.host.emit(self.name, "text", {"text": _cut(block.text, _TEXT_LIMIT)})
                elif isinstance(block, ThinkingBlock) and (block.thinking or "").strip():
                    # Reasoning is stored redacted; a thinking block that has
                    # text is the short progress note meant for the user.
                    self.host.emit(self.name, "text", {"text": _cut(block.thinking, _TEXT_LIMIT),
                                                      "thinking": True})
                elif isinstance(block, ToolUseBlock):
                    self.host.emit(self.name, "tool", {"id": block.id, "name": block.name,
                                                      "input": _cut(block.input, 2000)})
            if msg.error:
                self.host.emit(self.name, "notice", {"kind": "assistant_error",
                                                    "error": msg.error})
            return
        if isinstance(msg, ResultMessage):
            self._follow_sid(msg.session_id, strong=True)
            self._close_prompts(msg)
            self.host.store.set_session(
                self.name, inflight=encode_inflight(self.prompts), info=self.info)
            if msg.total_cost_usd is not None:
                self.info["cost"] = msg.total_cost_usd
            usage = msg.model_usage or {}
            window = max((int(v.get("contextWindow") or 0) for v in usage.values()), default=0)
            self.host.emit(self.name, "turn_end", {
                "subtype": msg.subtype, "is_error": msg.is_error,
                "result": _cut(msg.result or "", _TEXT_LIMIT),
                "terminal_reason": msg.terminal_reason, "stop_reason": msg.stop_reason,
                "api_error_status": msg.api_error_status, "errors": msg.errors,
                "cost": msg.total_cost_usd, "duration_ms": msg.duration_ms,
                "denials": len(msg.permission_denials or []), "context_window": window,
                "queued": len(self.prompts)})
            self._update_state()
            task = asyncio.create_task(self._metrics())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return
        if isinstance(msg, RateLimitEvent):
            raw = msg.rate_limit_info.raw or {}
            self.limits = raw.get("unifiedWindows") or self.limits
            self.host.emit(self.name, "metrics", {"limits": self.limits,
                                                 "status": msg.rate_limit_info.status},
                           journal=False)

    async def _metrics(self) -> None:
        """Context use after a turn: asked of the CLI, never in the reader."""
        try:
            cu = await self.call(lambda c: c.get_context_usage(), timeout=15)
        except Exception as exc:
            log.debug("[%s] context usage unavailable: %s", self.name, exc)
            return
        ctx = {k: cu.get(k) for k in ("totalTokens", "maxTokens", "percentage")}
        self.info["context"] = ctx
        self.host.store.set_session(self.name, info=self.info)
        self.host.emit(self.name, "metrics", {"context": ctx, "limits": self.limits,
                                             "model": self.info.get("model")}, journal=False)

    # -- questions and permissions ---------------------------------------
    async def _can_use_tool(self, tool: str, input_: dict[str, Any],
                            ctx: ToolPermissionContext):
        rid = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.requests[rid] = (fut, ctx)
        self.host.store.open_request(rid, self.name, self.sid, tool, input_)
        self.host.emit(self.name, "request_open", {
            "rid": rid, "tool": tool, "input": _cut(input_, _INPUT_LIMIT),
            "title": ctx.title, "display_name": ctx.display_name,
            "description": ctx.description, "reason": ctx.decision_reason,
            "blocked_path": ctx.blocked_path, "agent_id": ctx.agent_id,
            "suggestions": [_suggestion(s) for s in ctx.suggestions or []]})
        self._update_state()
        try:
            answer: dict[str, Any] = await fut
        except asyncio.CancelledError:
            if self.host.store.close_request(rid, "lost", None):
                self.host.emit(self.name, "request_closed", {"rid": rid, "how": "lost"})
            raise
        finally:
            self.requests.pop(rid, None)
            self._update_state()
        if answer.get("behavior") == "allow":
            updates: list[PermissionUpdate] = []
            for i in answer.get("suggestions") or []:
                if isinstance(i, int) and 0 <= i < len(ctx.suggestions or []):
                    updates.append(ctx.suggestions[i])
            if answer.get("set_mode"):
                updates.append(PermissionUpdate(type="setMode", mode=answer["set_mode"],
                                                destination="session"))
            # The mode this allow leaves the session in: an approved plan ends
            # plan mode, and a setMode update names its own.
            new_mode = next((str(u.mode) for u in updates
                             if getattr(u, "type", "") == "setMode" and u.mode), "")
            if not new_mode and tool == "ExitPlanMode":
                new_mode = "default"
            if new_mode and new_mode != self.info.get("mode"):
                self.note_mode(new_mode)
            return PermissionResultAllow(updated_input=answer.get("updated_input"),
                                         updated_permissions=updates or None)
        return PermissionResultDeny(
            message=answer.get("message") or "The user declined this in Telegram.",
            interrupt=bool(answer.get("interrupt")))

    def note_mode(self, mode: str) -> None:
        """The permission mode changed: remembered, and told to the bot."""
        self.info["mode"] = mode
        self.host.store.set_session(self.name, info=self.info)
        self.host.emit(self.name, "mode", {"mode": mode})

    def answer(self, rid: str, answer: dict[str, Any]) -> str:
        """Hand the chat's answer to the waiting callback: ok | already | unknown."""
        entry = self.requests.get(rid)
        if not self.host.store.close_request(rid, "answered", answer):
            row = self.host.store.request(rid)
            if row is None:
                return "unknown"
            return "already" if row["state"] == "answered" else str(row["state"])
        if entry is None or entry[0].done():
            return "unknown"
        entry[0].set_result(answer)
        self.host.emit(self.name, "request_closed", {"rid": rid, "how": "answered"})
        return "ok"
