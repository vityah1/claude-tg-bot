"""The bot's end of the socket to the session host (`ccbot/host/`).

It keeps one connection open, comes back after a dropped socket or a host
restart (with a growing pause, never a busy loop), and on every connect says
how far it has got (`last_seq`) so the host replays what was missed. Events
are handed to the bridge one at a time, in order; a command is a request with
an id and a deadline. The link never raises out of its own loop: a host that
is down is a state (`online` False) the bridge reports, not a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from .host import protocol

log = logging.getLogger("ccbot.hostlink")

_BACKOFF = (0.5, 1, 2, 5, 10, 15)


class HostError(Exception):
    """A command the host refused or could not run; `code` says which."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


class HostLink:
    def __init__(self,
                 on_event: Callable[[dict[str, Any]], Awaitable[None]],
                 on_welcome: Callable[[dict[str, Any]], Awaitable[None]],
                 last_seq: Callable[[], int]):
        self.on_event = on_event
        self.on_welcome = on_welcome
        self.last_seq = last_seq
        self.online = False
        self.epoch: str | None = None
        self.down_since: float | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._replies: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="hostlink")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        attempt = 0
        while True:
            try:
                await self._session()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except (ConnectionError, FileNotFoundError, OSError) as exc:
                if attempt == 0:
                    log.info("session host unreachable: %s", exc)
            except Exception:
                log.exception("host link failed")
            self._down()
            await asyncio.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
            attempt += 1

    def _down(self) -> None:
        if self.online:
            log.info("session host connection lost")
            self.down_since = asyncio.get_running_loop().time()
        self.online = False
        self._connected.clear()
        self._writer = None
        for fut in self._replies.values():
            if not fut.done():
                fut.set_exception(HostError("disconnected"))
        self._replies.clear()

    async def _session(self) -> None:
        reader, writer = await asyncio.open_unix_connection(
            str(protocol.socket_path()), limit=protocol.MAX_FRAME)
        try:
            writer.write(protocol.encode({"op": "hello", "proto": protocol.PROTO,
                                          "last_seq": self.last_seq()}))
            await writer.drain()
            welcome = protocol.decode(await asyncio.wait_for(reader.readline(), 15))
            if welcome.get("type") != "welcome":
                raise ConnectionError("no welcome from the host")
            self._writer = writer
            self.online = True
            self.down_since = None
            self._connected.set()
            self.epoch = welcome.get("epoch")
            log.info("session host connected (epoch %s, head %s)", self.epoch,
                     welcome.get("head"))
            await self.on_welcome(welcome)
            while True:
                line = await reader.readline()
                if not line:
                    raise ConnectionError("host closed the socket")
                frame = protocol.decode(line)
                if frame.get("type") == "reply":
                    fut = self._replies.pop(str(frame.get("re")), None)
                    if fut is not None and not fut.done():
                        fut.set_result(frame)
                    continue
                if frame.get("type") == "event":
                    # Awaited, not spawned: the order of events is the order
                    # of the session, and a card must not overtake its text.
                    try:
                        await self.on_event(frame)
                    except Exception:
                        log.exception("event %s not handled", frame.get("ev"))
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def ack(self, seq: int) -> None:
        if self._writer is None:
            return
        with contextlib.suppress(Exception):
            self._writer.write(protocol.encode({"op": "ack", "seq": seq}))

    async def call(self, op: str, timeout: float = 30.0, wait_online: float = 5.0,
                   **params: Any) -> dict[str, Any]:
        """Run a command on the host; its `data`, or HostError."""
        if not self.online:
            try:
                await asyncio.wait_for(self._connected.wait(), wait_online)
            except TimeoutError:
                raise HostError("offline") from None
        writer = self._writer
        if writer is None:
            raise HostError("offline")
        cid = uuid.uuid4().hex[:12]
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._replies[cid] = fut
        try:
            writer.write(protocol.encode({"op": op, "id": cid, **params}))
            await writer.drain()
        except OSError:
            # The socket went between the online check and the write.
            self._replies.pop(cid, None)
            raise HostError("offline") from None
        try:
            reply = await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            self._replies.pop(cid, None)
            raise HostError("timeout", op) from None
        if not reply.get("ok"):
            err = reply.get("error") or {}
            raise HostError(str(err.get("code") or "error"), str(err.get("message") or ""))
        return reply.get("data") or {}
