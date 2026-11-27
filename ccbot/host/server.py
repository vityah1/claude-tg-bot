"""The host process: the socket, the journal, and the registry of sessions.

One client at a time (the bot); a second connection replaces the first, the
same way Telegram allows one poller. Every command is answered, success or
failure, and a failure never takes the host down: a command handler that
raises is logged with its stack and answered with an error code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from importlib import metadata
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
)

from . import protocol
from .actor import (
    CLAUDE_CODE_PROMPT,
    MAX_MESSAGE_BYTES,
    SessionActor,
    StartError,
    claude_bin,
    decode_inflight,
)
from .store import HostStore

log = logging.getLogger("ccbot.host")

# Commands answered with a stored result when the bot repeats them after a
# dropped socket, so a prompt is never sent twice.
_DEDUP_KEEP = 500
# A bot that does not read: past this many unsent frames it is dropped and
# replays from its last ack when it comes back.
_OUT_MAX = 20_000
# A side question (/btw) gives up before the bot stops waiting for it.
_SIDE_TIMEOUT = 540.0


def _sdk_version() -> str:
    try:
        return metadata.version("claude-agent-sdk")
    except metadata.PackageNotFoundError:
        return "?"


class CommandError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message


class Host:
    def __init__(self, store: HostStore | None = None):
        self.store = store or HostStore()
        self.epoch = uuid.uuid4().hex[:12]
        self.started = time.time()
        self.actors: dict[str, SessionActor] = {}
        self.out: asyncio.Queue[bytes] | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.done: dict[str, dict[str, Any]] = {}
        self.server: asyncio.Server | None = None
        self.starting: dict[str, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()
        # Set when the host itself is stopping: the `claude` children get the
        # same signal, and their exit then is not a session's fault.
        self.shutting_down = False

    # -- events ----------------------------------------------------------
    def emit(self, name: str | None, type_: str, data: dict[str, Any],
             journal: bool = True) -> None:
        """Record an event and pass it on to the bot, if one is listening.

        *journal* False is for what is only worth seeing live (a state flip,
        metrics, a status line): a bot that was away gets the current
        picture from the welcome snapshot instead.
        """
        frame: dict[str, Any] = {"type": "event", "ev": type_, "name": name,
                                 "ts": time.time(), "data": data}
        if journal:
            frame["seq"] = self.store.append(name, type_, data)
        self._push(frame)

    def _push(self, frame: dict[str, Any]) -> None:
        if self.out is None:
            return
        if self.out.qsize() > _OUT_MAX:
            log.warning("bot is not reading; dropping the connection")
            self._drop_client()
            return
        self.out.put_nowait(protocol.encode(frame))

    def _drop_client(self) -> None:
        self.out = None
        if self.writer is not None:
            with contextlib.suppress(Exception):
                self.writer.close()
        self.writer = None

    # -- startup ---------------------------------------------------------
    def recover(self) -> None:
        """Account for what the previous host process left behind.

        Every session it ran is dormant now (its `claude` died with it), a
        question it had open can no longer be answered, and a turn it was in
        the middle of is lost — the chat is told all three, once.
        """
        lost = [{"rid": r["rid"], "name": r["name"], "tool": r["tool"]}
                for r in self.store.lose_open_requests()]
        dormant, interrupted, background = [], [], []
        for row in self.store.sessions():
            if row.state == "live":
                dormant.append(row.name)
                self.store.set_session(row.name, state="dormant")
            prompts: list[str] = []
            if row.inflight:
                prompts = [p.text for p in decode_inflight(row.inflight)]
                if prompts:
                    interrupted.append({"name": row.name, "prompts": prompts})
                else:
                    log.info("dropped an unreadable or older in-flight record of %s",
                             row.name)
                self.store.set_session(row.name, inflight=None)
            info = row.info or {}
            tasks = info.get("background") or []
            if tasks:
                # Its background tasks died with the old process: said once.
                background.append({"name": row.name, "tasks": tasks,
                                   "interrupted": bool(prompts)})
                self.store.set_session(row.name, info={k: v for k, v in info.items()
                                                       if k != "background"})
        if dormant or lost or interrupted or background:
            log.info("recovered: %d dormant, %d lost request(s), %d interrupted, "
                     "%d with background tasks cut", len(dormant), len(lost),
                     len(interrupted), len(background))
            self.emit(None, "host_restarted", {"dormant": dormant, "lost": lost,
                                               "interrupted": interrupted,
                                               "background": background,
                                               "epoch": self.epoch})
        acked = int(self.store.meta_get("acked") or 0)
        self.store.prune(acked)

    # -- sessions --------------------------------------------------------
    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for row in self.store.sessions():
            actor = self.actors.get(row.name)
            out.append({
                "name": row.name, "sid": actor.sid if actor else row.sid, "cwd": row.cwd,
                "state": actor.state if actor else row.state,
                "prompts": len(actor.prompts) if actor else 0,
                "info": (actor.info if actor else row.info) or {},
                "limits": actor.limits if actor else {},
            })
        return out

    def open_requests(self) -> list[dict[str, Any]]:
        out = []
        for r in self.store.open_requests():
            try:
                input_ = json.loads(r["input"])
            except ValueError:
                input_ = {}
            out.append({"rid": r["rid"], "name": r["name"], "tool": r["tool"],
                        "input": input_, "opened_at": r["opened_at"]})
        return out

    def actor_ended(self, actor: SessionActor, how: str) -> None:
        if self.actors.get(actor.name) is actor:
            del self.actors[actor.name]
        info: dict[str, Any] | None = None
        if "background" in actor.info and (how == "closed" or not self.shutting_down):
            # Ended for good or by a fault: its tasks are gone, nothing to
            # report at the next start. A shutdown keeps them for `recover()`.
            actor.info.pop("background", None)
            info = actor.info
        if how == "closed":
            self.store.set_session(actor.name, state="closed", inflight=None,
                                   **({"info": info} if info is not None else {}))
        else:
            self.store.set_session(actor.name, state="dormant",
                                   **({"info": info} if info is not None else {}))
        self.emit(actor.name, "session_state", {"state": how})

    async def _start(self, name: str, sid: str, cwd: str, resume: bool,
                     model: str | None = None) -> SessionActor:
        row = self.store.session(name)
        chosen = (row.info if row else {}).get("chosen") or {}
        actor = SessionActor(self, name, sid, cwd)
        actor.info["chosen"] = chosen
        self.actors[name] = actor
        self.store.upsert_session(name, sid, cwd, "live")
        try:
            await actor.start(resume, model or chosen.get("model"), chosen.get("mode"),
                              chosen.get("effort"))
            if chosen.get("effort"):
                actor.info["effort"] = chosen["effort"]
        except StartError as exc:
            if self.actors.get(name) is actor:
                del self.actors[name]
            self.store.set_session(name, state="dormant" if resume else "closed")
            raise CommandError("start_failed", str(exc)) from exc
        return actor

    async def _actor(self, name: str, wake: bool = True) -> SessionActor:
        """The running actor of a session; a dormant one is resumed first."""
        lock = self.starting.setdefault(name, asyncio.Lock())
        async with lock:
            actor = self.actors.get(name)
            if actor is not None and actor.state not in ("dormant", "closed"):
                return actor
            row = self.store.session(name)
            if row is None or row.state == "closed":
                raise CommandError("no_session", name)
            if not wake:
                raise CommandError("dormant", name)
            log.info("waking %s (resume %s)", name, row.sid[:8])
            return await self._start(name, row.sid, row.cwd, resume=True)

    # -- commands --------------------------------------------------------
    async def cmd_create(self, c: dict[str, Any]) -> dict[str, Any]:
        name, cwd, sid = c["name"], c["cwd"], c["sid"]
        row = self.store.session(name)
        if name in self.actors or (row and row.state != "closed" and row.sid != sid):
            raise CommandError("exists", name)
        actor = await self._start(name, sid, cwd, bool(c.get("resume")), c.get("model"))
        return {"sid": actor.sid, "info": actor.info}

    async def cmd_send(self, c: dict[str, Any]) -> dict[str, Any]:
        actor = await self._actor(c["name"])
        if c.get("guard") and actor.asking:
            raise CommandError("request_open")
        prompt, waits = await actor.send(c["text"])
        return {"state": actor.state, "queued": len(actor.prompts), "uuid": prompt.uuid,
                "waiting": waits and not prompt.taken}

    async def cmd_cancel(self, c: dict[str, Any]) -> dict[str, Any]:
        """Take back a prompt the session has not taken into a turn yet."""
        try:
            actor = await self._actor(c["name"], wake=False)
        except CommandError:
            return {"cancelled": False, "why": "unknown"}
        return await actor.cancel(str(c["uuid"]))

    async def cmd_answer(self, c: dict[str, Any]) -> dict[str, Any]:
        rid = c["rid"]
        for actor in self.actors.values():
            if rid in actor.requests:
                return {"result": actor.answer(rid, c["answer"])}
        row = self.store.request(rid)
        if row is None:
            return {"result": "unknown"}
        return {"result": "already" if row["state"] == "answered" else row["state"]}

    async def cmd_interrupt(self, c: dict[str, Any]) -> dict[str, Any]:
        actor = await self._actor(c["name"], wake=False)
        await actor.call(lambda cl: cl.interrupt(), timeout=20)
        return {}

    async def cmd_set_mode(self, c: dict[str, Any]) -> dict[str, Any]:
        actor = await self._actor(c["name"])
        await actor.call(lambda cl: cl.set_permission_mode(c["mode"]), timeout=20)
        actor.info.setdefault("chosen", {})["mode"] = c["mode"]
        actor.note_mode(c["mode"])
        return {}

    async def cmd_set_model(self, c: dict[str, Any]) -> dict[str, Any]:
        actor = await self._actor(c["name"])
        await actor.call(lambda cl: cl.set_model(c.get("model")), timeout=20)
        actor.info.setdefault("chosen", {})["model"] = c.get("model")
        self.store.set_session(actor.name, info=actor.info)
        return {}

    async def cmd_set_effort(self, c: dict[str, Any]) -> dict[str, Any]:
        """/effort through the CLI, remembered so a wake-up or restart keeps it."""
        actor = await self._actor(c["name"])
        if actor.asking:
            raise CommandError("request_open")
        level = str(c["effort"])
        await actor.send(f"/effort {level}")
        actor.info.setdefault("chosen", {})["effort"] = level
        actor.info["effort"] = level
        self.store.set_session(actor.name, info=actor.info)
        self.emit(actor.name, "effort", {"effort": level})
        return {}

    async def cmd_info(self, c: dict[str, Any]) -> dict[str, Any]:
        actor = await self._actor(c["name"])
        return {"server": await actor.call(lambda cl: cl.get_server_info(), timeout=20)}

    async def cmd_side(self, c: dict[str, Any]) -> dict[str, Any]:
        """A side question (/btw): asked of a fork, so the session is untouched.

        The fork resumes the session's transcript under a new id, answers, and
        is left. It may not run tools — a side question is a question — and
        its id is returned so the bot can keep it out of history.
        """
        row = self.store.session(c["name"])
        if row is None or row.state == "closed":
            raise CommandError("no_session", c["name"])
        actor = self.actors.get(c["name"])
        sid = actor.sid if actor else row.sid

        async def no_tools(tool, input_, ctx):
            return PermissionResultDeny(message="This is a side question: answer from "
                                                "what you already know, without tools.")

        async def deny_hook(input_data, tool_use_id, context):
            # Before any allow rule or mode: a side question runs no tools.
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Side question: answer without tools."}}

        # Everything that opens the prompt — the model, the mode, the tool list
        # — is kept as the session has it, so the fork reads the session's
        # cached context (~10% of the price) instead of writing it anew
        # (measured 2026-11-24 on 105k tokens: 108k read / 163 written,
        # against 57k read / 92k written with the tools taken away).
        info = (actor.info if actor else row.info) or {}
        chosen = info.get("chosen") or {}
        model = chosen.get("model") or info.get("model") or None
        if model == protocol.SYNTHETIC_MODEL:
            model = None        # recorded by an older host
        mode = info.get("mode") if info.get("mode") in (
            "default", "acceptEdits", "plan", "auto", "bypassPermissions", "dontAsk") else None
        opts = ClaudeAgentOptions(
            cwd=row.cwd, cli_path=claude_bin(), resume=sid, fork_session=True,
            system_prompt=CLAUDE_CODE_PROMPT, max_buffer_size=MAX_MESSAGE_BYTES,
            model=model, permission_mode=mode,  # pyright: ignore[reportArgumentType]
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[deny_hook])]},  # pyright: ignore[reportArgumentType]
            can_use_tool=no_tools)
        texts: list[str] = []
        fork = ""

        async def ask() -> None:
            nonlocal fork
            async with ClaudeSDKClient(opts) as client:
                await client.query(c["text"])
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage) and not msg.parent_tool_use_id:
                        texts.extend(b.text for b in msg.content
                                     if isinstance(b, TextBlock) and b.text.strip())
                    if isinstance(msg, ResultMessage):
                        fork = msg.session_id
                        log.info("side question for %s: usage %s", c["name"], {
                            k: (msg.usage or {}).get(k) for k in (
                                "cache_read_input_tokens", "cache_creation_input_tokens",
                                "output_tokens")})

        try:
            await asyncio.wait_for(ask(), _SIDE_TIMEOUT)
        except TimeoutError:
            raise CommandError("timeout", "the side question took too long") from None
        return {"text": "\n\n".join(texts).strip(), "fork": fork}

    async def cmd_close(self, c: dict[str, Any]) -> dict[str, Any]:
        name = c["name"]
        actor = self.actors.get(name)
        if actor is not None:
            await actor.close()
        else:
            self.store.set_session(name, state="closed", inflight=None)
        return {}

    async def cmd_restart(self, c: dict[str, Any]) -> dict[str, Any]:
        """Leave the client and resume the same id: a new CLI from disk."""
        name = c["name"]
        actor = self.actors.get(name)
        if actor is not None:
            if (actor.asking or actor.prompts) and not c.get("force"):
                raise CommandError("busy", actor.state)
            await actor.close(end=False)
            self.actors.pop(name, None)
            self.store.set_session(name, state="dormant")
        new = await self._actor(name)
        return {"sid": new.sid, "info": new.info}

    async def cmd_list(self, c: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": self.snapshot(), "open_requests": self.open_requests()}

    async def cmd_status(self, c: dict[str, Any]) -> dict[str, Any]:
        return {"pid": os.getpid(), "epoch": self.epoch, "started": self.started,
                "sdk": _sdk_version(), "cli": claude_bin(), "head": self.store.head(),
                "acked": int(self.store.meta_get("acked") or 0),
                "live": sum(1 for a in self.actors.values()
                            if a.state not in ("dormant", "closed")),
                "busy": [a.name for a in self.actors.values() if a.prompts or a.asking]}

    async def cmd_ping(self, c: dict[str, Any]) -> dict[str, Any]:
        return {"epoch": self.epoch}

    async def _command(self, c: dict[str, Any]) -> None:
        cid = str(c.get("id") or "")
        op = str(c.get("op") or "")
        dedup = c.get("cmd_id")
        if dedup and dedup in self.done:
            self._push({"type": "reply", "re": cid, **self.done[dedup]})
            return
        handler = getattr(self, f"cmd_{op}", None)
        try:
            if handler is None:
                raise CommandError("unknown_op", op)
            data = await handler(c)
            reply: dict[str, Any] = {"ok": True, "data": data}
        except CommandError as exc:
            log.info("command %s refused: %s %s", op, exc.code, exc.message)
            reply = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
        except KeyError as exc:
            reply = {"ok": False, "error": {"code": "bad_request", "message": f"missing {exc}"}}
        except Exception as exc:
            log.exception("command %s failed", op)
            reply = {"ok": False, "error": {"code": "internal",
                                            "message": f"{type(exc).__name__}: {exc}"[:500]}}
        if dedup:
            self.done[dedup] = reply
            while len(self.done) > _DEDUP_KEEP:
                self.done.pop(next(iter(self.done)))
        self._push({"type": "reply", "re": cid, **reply})

    # -- the connection --------------------------------------------------
    async def _client(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            hello = protocol.decode(line)
        except Exception:
            writer.close()
            return
        if hello.get("op") != "hello":
            writer.close()
            return
        if self.writer is not None:
            log.info("a new client replaces the old one")
            self._drop_client()
        last = int(hello.get("last_seq") or 0)
        oldest = self.store.oldest()
        welcome = {"type": "welcome", "proto": protocol.PROTO, "epoch": self.epoch,
                   "head": self.store.head(), "gap": bool(last and oldest and last < oldest - 1),
                   "sdk": _sdk_version(), "sessions": self.snapshot(),
                   "open_requests": self.open_requests()}
        out: asyncio.Queue[bytes] = asyncio.Queue()
        out.put_nowait(protocol.encode(welcome))
        # Replay and going live happen with no await in between, so no event
        # can fall into the gap between the two. Paged, to the very head.
        cursor = last
        while True:
            rows = self.store.since(cursor)
            if not rows:
                break
            for row in rows:
                out.put_nowait(protocol.encode({
                    "type": "event", "seq": row["seq"], "ev": row["type"],
                    "name": row["name"], "ts": row["ts"], "data": json.loads(row["json"]),
                    "replay": True}))
            cursor = rows[-1]["seq"]
        self.out, self.writer = out, writer
        log.info("bot connected (last_seq=%d, head=%d)", last, self.store.head())
        sender = asyncio.create_task(self._send_loop(out, writer))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    c = protocol.decode(line)
                except ValueError:
                    log.warning("unreadable frame from the bot")
                    continue
                if c.get("op") == "ack":
                    self._ack(int(c.get("seq") or 0))
                    continue
                task = asyncio.create_task(self._command(c))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            sender.cancel()
            if self.writer is writer:
                self._drop_client()
            log.info("bot disconnected")

    async def _send_loop(self, out: asyncio.Queue[bytes],
                         writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await out.get()
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _ack(self, seq: int) -> None:
        if seq <= int(self.store.meta_get("acked") or 0):
            return
        self.store.meta_set("acked", str(seq))
        if seq % 200 == 0:
            self.store.prune(seq)

    # -- run -------------------------------------------------------------
    async def serve(self) -> None:
        path = protocol.socket_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        self.recover()
        old = os.umask(0o177)
        try:
            self.server = await asyncio.start_unix_server(
                self._client, path=str(path), limit=protocol.MAX_FRAME)
        finally:
            os.umask(old)
        log.info("host %s listening on %s (sdk %s, cli %s)", self.epoch, path,
                 _sdk_version(), claude_bin())
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        def on_signal() -> None:
            self.shutting_down = True
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, on_signal)
        await stop.wait()
        self.shutting_down = True
        log.info("host stopping: %d session(s)", len(self.actors))
        self.server.close()
        await asyncio.gather(*(self._stop_actor(a) for a in list(self.actors.values())),
                             return_exceptions=True)

    async def _stop_actor(self, actor: SessionActor) -> None:
        """Leave a session at host shutdown: it is dormant, not ended."""
        await actor.close(end=False)
        self.store.set_session(actor.name, state="live")
