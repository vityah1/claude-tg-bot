"""The bot's side of the sessions: host events in, chat messages and cards out.

Every session is held by the session host (`ccbot/host/`); this follows it
through the host's events by these rules:

* text is buffered and flushed once the session has been quiet for
  `_FLUSH_IDLE`, or the buffer is `_MAX_BUFFER_AGE` old, or a turn ended, or
  a question arrived — the text always goes out ahead of its question;
* while a question card stands, nothing of that session lands under it;
* a question, a permission request and a plan are cards built from the
  request's data (`can_use_tool`), never from a screen;
* every message is registered (`remember_message`), so a reply addresses
  the session it came from.

An event's effect has reached the chat when it is sent; only then is its
`seq` acknowledged, so a bot restart replays exactly what had not gone out.
A card already sent for a request is not sent again (`request_cards`).
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import rich
from .hostlink import HostError, HostLink
from .i18n import _, ngettext
from .i18n import use as use_locale
from .keyboards import done_kb, menu_kb
from .sessions import clean_prompt
from .state import Managed, Store
from .status_feed import Limit, Usage
from .transcript import _summarise_tool
from .util import split_text

if TYPE_CHECKING:
    from .watcher import Watcher

log = logging.getLogger("ccbot.bridge")

_FLUSH_IDLE = 2.0
_MAX_BUFFER_AGE = 25.0
_HOLD_MAX = 300.0
_PULSE_AFTER = 75.0
_PULSE_EDIT = 45.0
_TICK = 1.0
_FAIL_REPEAT = 3600.0
# How long the chat is told nothing about a host that is down: a host
# restart takes a few seconds and is not news.
_DOWN_GRACE = 30.0
# Tools whose call is the card itself: a "🔧" line for them says nothing.
_CARD_TOOLS = {"AskUserQuestion", "ExitPlanMode"}


@dataclass
class _Rt:
    """Per-session bookkeeping, in memory only."""

    buffer: list[str] = field(default_factory=list)
    seqs: set[int] = field(default_factory=set)
    buffer_since: float = 0.0
    state: str = "idle"
    turn_texts: int = 0
    work_since: float = 0.0
    pulse_msg_id: int | None = None
    pulse_edited: float = 0.0
    info: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    cost: float = 0.0
    metrics_at: float = 0.0
    # Usage alert steps already announced (`Watcher._check_usage` keeps them here).
    alerted: dict[str, int] = field(default_factory=dict)
    # Metrics arrived while a card held the session: checked once it is free.
    usage_due: bool = False

    @property
    def account(self) -> str:
        """The account the host reports for the session, for the limit alerts."""
        return str(self.info.get("account") or "")


@dataclass
class _Req:
    """An open question, permission request or plan of an SDK session."""

    rid: str
    name: str
    tool: str
    input: dict[str, Any]
    data: dict[str, Any]
    msg_id: int | None = None
    at: float = field(default_factory=time.monotonic)
    # When a card was last attempted: one that did not go out is tried again.
    tried: float = 0.0
    # «🔔 Show the question» asked for it at the bottom; the tick posts it.
    repost: bool = False
    # A send of its card is under way: no second path may start another
    # (the tick's retry raced the event's own send — two cards, 2026-11-24).
    sending: bool = False
    # AskUserQuestion: the part on show, what has been answered, the ticks.
    index: int = 0
    answers: dict[str, Any] = field(default_factory=dict)
    ticks: set[int] = field(default_factory=set)

    @property
    def kind(self) -> str:
        if self.tool == "AskUserQuestion":
            return "question"
        if self.tool == "ExitPlanMode":
            return "plan"
        return "permission"

    @property
    def questions(self) -> list[dict[str, Any]]:
        qs = self.input.get("questions")
        return qs if isinstance(qs, list) else []


class SdkBridge:
    def __init__(self, bot: Bot, store: Store, watcher: Watcher, chat_id: int):
        self.bot = bot
        self.store = store
        self.watcher = watcher
        self.chat_id = chat_id
        self.rt: dict[str, _Rt] = {}
        self.requests: dict[str, _Req] = {}
        self.acked = int(store.meta_get("host_seq") or 0)
        self.seen = self.acked
        # Seqs past the ack point that have already reached the chat. The ack
        # waits for the oldest buffered event of any session, so a replay after
        # a restart starts there — and must skip what went out meanwhile.
        try:
            self.delivered: set[int] = {int(x) for x in json.loads(
                store.meta_get("host_delivered") or "[]") if int(x) > self.acked}
        except (ValueError, TypeError):
            self.delivered = set()
        self.link = HostLink(self._on_event, self._on_welcome, lambda: self.acked)
        self._task: asyncio.Task | None = None
        # The ids of the session rows, refreshed each tick: `usage_for` sits on
        # the hot path of every `status_feed.read` and must not query for
        # an id that is no session of ours.
        self.sdk_ids: set[str] = set()
        self.tasks: set[asyncio.Task] = set()
        self._fail_sig: str | None = None
        self._fail_told = 0.0
        self._down_told = False
        self.epoch: str | None = store.meta_get("host_epoch")
        # Prompts a host restart or a crash cut off, offered again by
        # «🔁 Send again» (`again:<n>`): (launch name, text).
        self.again: dict[int, tuple[str, str]] = {}
        try:
            saved = json.loads(store.meta_get("sdk_again") or "{}")
            self.again = {int(k): (str(v[0]), str(v[1])) for k, v in saved.items()}
        except (ValueError, TypeError, IndexError):
            self.again = {}
        # Prompts a turn has taken in (`prompt_taken`, uuid -> when): the
        # event can beat the confirmation it would update.
        self.taken: dict[str, float] = {}
        self.on_taken: Callable[[str, str], Awaitable[None]] | None = None
        # Never handed out twice: an old button must not reach a newer text.
        self.again_next = int(store.meta_get("sdk_again_next") or 0) or \
            (max(self.again, default=0) + 1)

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self.link.start()
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="bridge")

    async def stop(self) -> None:
        await self.link.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    def _rt(self, name: str) -> _Rt:
        rt = self.rt.get(name)
        if rt is None:
            rt = self.rt[name] = _Rt()
        return rt

    # -- commands for bot.py ---------------------------------------------
    async def create(self, mgd: Managed, resume: bool) -> dict[str, Any]:
        return await self.link.call("create", timeout=150, name=mgd.name, cwd=mgd.cwd,
                                    sid=mgd.session_id, resume=resume)

    async def send(self, mgd: Managed, text: str, guard: bool = False) -> dict[str, Any]:
        """A prompt; HostError("request_open") when a question is open and *guard*.

        The reply carries the prompt's `uuid` and whether it is `waiting` for
        the running turn to take it in — until then it can be taken back.
        """
        self._rt(mgd.name).work_since = self._rt(mgd.name).work_since or time.time()
        return await self.link.call("send", timeout=150, name=mgd.name, text=text,
                                    guard=guard) or {}

    async def cancel(self, mgd: Managed, uid: str) -> dict[str, Any]:
        """Take back a prompt no turn has taken in: {"cancelled", "why"}."""
        return await self.link.call("cancel", timeout=30, name=mgd.name, uuid=uid) or {}

    def side(self, mgd: Managed, question: str) -> None:
        """A /btw for an SDK session: answered off the turn, in the background."""
        task = asyncio.create_task(self._side(mgd, question))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _side(self, mgd: Managed, question: str) -> None:
        with use_locale(self.watcher._locale()):
            try:
                data = await self.link.call("side", timeout=600, name=mgd.name, text=question)
            except HostError as exc:
                log.warning("side question failed id=%s: %s", mgd.session_id[:8], exc)
                await self._say(_("⚠️ The side question to <b>{name}</b> got no answer: "
                                  "<code>{error}</code>").format(
                                      name=html.escape(mgd.full_label),
                                      error=html.escape(str(exc)[:300])),
                                mgd.session_id, parse_mode="HTML")
                return
            except Exception as exc:
                log.exception("side question crashed")
                await self._report_failure(exc)
                return
            if data.get("fork"):
                self.store.hide_session(str(data["fork"]))
            title = _("🗨 Side question (/btw): {question}").format(
                question=rich.md_escape(question[:300]))
            body = str(data.get("text") or _("(no answer)"))
            await self.watcher._emit(mgd.session_id, mgd.full_label, f"_{title}_\n\n{body}",
                                     self.usage_for(mgd.session_id))

    async def interrupt(self, mgd: Managed) -> None:
        await self.link.call("interrupt", name=mgd.name)

    async def close(self, mgd: Managed) -> None:
        await self.link.call("close", timeout=60, name=mgd.name)
        self.forget(mgd.name)

    def forget(self, name: str) -> None:
        self.rt.pop(name, None)
        for rid in [r for r, q in self.requests.items() if q.name == name]:
            self.requests.pop(rid, None)

    def request_repost(self, mgd: Managed) -> bool:
        """Have the session's open cards posted again at the bottom; False: none."""
        reqs = [q for q in self.requests.values() if q.name == mgd.name]
        for q in reqs:
            if q.msg_id is None:
                q.tried = 0.0       # its card never went out: the next tick sends it
            else:
                q.repost = True
        return bool(reqs)

    def question_open(self, mgd: Managed) -> bool:
        return any(q.name == mgd.name for q in self.requests.values())

    async def dismiss(self, mgd: Managed, message: str) -> int:
        """Decline every open request of the session (the held text's «close»)."""
        n = 0
        for req in [q for q in self.requests.values() if q.name == mgd.name]:
            await self._answer(req, {"behavior": "deny", "message": message},
                               _("⏹ Closed to send a message"))
            n += 1
        return n

    def state(self, mgd: Managed) -> str:
        rt = self.rt.get(mgd.name)
        if not self.link.online:
            return "offline"
        if self.question_open(mgd):
            return "waiting"
        return rt.state if rt else "dormant"

    def info(self, mgd: Managed) -> _Rt | None:
        return self.rt.get(mgd.name)

    async def _check_usage(self, name: str, rt: _Rt) -> None:
        mgd = self.store.by_name(name)
        usage = self.usage_for(mgd.session_id) if mgd else None
        if mgd is not None and usage is not None:
            await self.watcher._check_usage(mgd.full_label, mgd.session_id, rt, usage)

    def _forget_context(self, rt: _Rt) -> None:
        """/clear and /compact empty the context: the old figure is no longer true."""
        rt.context = {}
        rt.info.pop("context", None)
        rt.alerted.pop("ctx", None)

    def mode(self, mgd: Managed) -> str:
        """The permission mode in force, as the session card names modes."""
        rt = self.rt.get(mgd.name)
        mode = str((rt.info if rt else {}).get("mode") or "")
        return "manual" if mode == "default" else mode

    def usage_for(self, session_id: str) -> Usage | None:
        """A session's metrics as `status_feed.Usage`."""
        if session_id not in self.sdk_ids:
            return None
        mgd = self.store.get(session_id)
        if mgd is None:
            return None
        rt = self.rt.get(mgd.name)
        if rt is None:
            return None
        ctx = rt.context or rt.info.get("context") or {}
        limits = []
        for key, val in (rt.limits or {}).items():
            if isinstance(val, dict) and val.get("utilization") is not None:
                limits.append(Limit(key=key, pct=round(float(val["utilization"]) * 100),
                                    resets_at=val.get("resetsAt")))
        order = {"five_hour": 0, "seven_day": 1}
        limits.sort(key=lambda lim: (order.get(lim.key, 2), lim.key))
        model = str(rt.info.get("model") or "")
        pct = ctx.get("percentage")
        return Usage(
            session_id=session_id, session_name=mgd.name, model=_model_name(model),
            model_id=model,
            version=str(rt.info.get("version") or ""), cwd=mgd.cwd,
            effort=str(rt.info.get("effort") or ""),
            ctx_pct=round(float(pct)) if pct is not None else None,
            ctx_window=int(ctx.get("maxTokens") or 0),
            input_tokens=int(ctx.get("totalTokens") or 0),
            cost_usd=rt.cost or float(rt.info.get("cost") or 0.0), limits=limits)

    # -- the loop: flushes, pulse, host-down notice ------------------------
    async def _loop(self) -> None:
        while True:
            with use_locale(self.watcher._locale()):
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("bridge tick failed")
                    await self._report_failure(exc)
            await asyncio.sleep(_TICK)

    def _refresh_ids(self) -> None:
        self.sdk_ids = {m.session_id for m in self.store.all_managed()}

    async def _tick(self) -> None:
        now = time.time()
        self._refresh_ids()
        await self._check_down()
        for name, rt in list(self.rt.items()):
            if rt.usage_due and not self._holding(name):
                rt.usage_due = False
                await self._check_usage(name, rt)
        for req in list(self.requests.values()):
            if req.rid not in self.requests:
                continue            # answered by an earlier await of this tick
            if req.repost and req.msg_id is not None:
                req.repost = False
                old, req.msg_id = req.msg_id, None
                await self._card(req, fresh=True)
                new = req.msg_id
                if new is None:
                    req.msg_id = old        # not sent: the old card stays in charge
                elif req.rid not in self.requests:
                    # Answered on the old card while the new one was going out.
                    await self._retire(req, _("⏹ Question closed"))
                else:
                    with contextlib.suppress(Exception):
                        await self.bot.edit_message_reply_markup(
                            chat_id=self.chat_id, message_id=old,
                            reply_markup=done_kb(_("⬇️ Shown again below")))
            elif req.msg_id is None and time.monotonic() - req.tried > 15:
                await self._card(req)
        for name, rt in list(self.rt.items()):
            if rt.buffer and not self._holding(name):
                age = now - rt.buffer_since
                quiet = rt.state != "busy"
                if (quiet and age >= _FLUSH_IDLE) or age >= _MAX_BUFFER_AGE:
                    await self._flush(name)
            await self._pulse(name, rt, now)
        self._ack()

    def _holding(self, name: str) -> bool:
        return any(q.name == name and q.msg_id is not None
                   and time.monotonic() - q.at < _HOLD_MAX
                   for q in self.requests.values())

    async def _check_down(self) -> None:
        if self.link.online:
            if self._down_told:
                self._down_told = False
                await self._say(_("✅ The session host is back — SDK sessions are "
                                  "reachable again."))
            return
        if self._down_told or not self.store.all_managed():
            return
        since = self.link.down_since
        loop_now = asyncio.get_running_loop().time()
        if since is None or loop_now - since < _DOWN_GRACE:
            if since is None:
                # Never connected since the bot started.
                self.link.down_since = loop_now
            return
        self._down_told = True
        await self._say(_("⚠️ <b>The session host is not answering.</b>\n"
                          "The sessions cannot be reached until it is back. It "
                          "restarts by itself — if this lasts, see <code>journalctl "
                          "--user -u claude-tg-host</code>."), parse_mode="HTML")

    async def _report_failure(self, exc: Exception) -> None:
        sig = f"{type(exc).__name__}: {exc}"
        now = time.time()
        if sig == self._fail_sig and now - self._fail_told < _FAIL_REPEAT:
            return
        self._fail_sig, self._fail_told = sig, now
        await self._say(_("⚠️ <b>The SDK session bridge has crashed</b>\n"
                          "<code>{error}</code>\n\nDetails in /log.").format(
                              error=html.escape(sig[:300])), parse_mode="HTML")

    async def _pulse(self, name: str, rt: _Rt, now: float) -> None:
        if rt.state != "busy" or self._holding(name):
            await self._drop_pulse(rt)
            return
        if not rt.work_since:
            rt.work_since = now
        if now - rt.work_since < _PULSE_AFTER:
            return
        mgd = self.store.by_name(name)
        if mgd is None:
            return
        minutes = int((now - rt.work_since) // 60)
        text = _("⏳ <b>{name}</b>\nworking for {minutes} min").format(
            name=html.escape(mgd.full_label), minutes=minutes)
        if rt.pulse_msg_id is None:
            msg = await self._say(text, mgd.session_id, parse_mode="HTML")
            rt.pulse_msg_id = getattr(msg, "message_id", None)
            rt.pulse_edited = now
            return
        if now - rt.pulse_edited < _PULSE_EDIT:
            return
        rt.pulse_edited = now
        with contextlib.suppress(Exception):
            await self.bot.edit_message_text(text, chat_id=self.chat_id,
                                             message_id=rt.pulse_msg_id, parse_mode="HTML")

    async def _drop_pulse(self, rt: _Rt) -> None:
        if rt.pulse_msg_id is None:
            return
        msg_id, rt.pulse_msg_id = rt.pulse_msg_id, None
        with contextlib.suppress(Exception):
            await self.bot.delete_message(chat_id=self.chat_id, message_id=msg_id)

    # -- acks ------------------------------------------------------------
    def _buffered(self) -> set[int]:
        return {s for rt in self.rt.values() for s in rt.seqs}

    def _deliver(self, seqs) -> None:
        """These events have reached the chat."""
        fresh = {s for s in seqs if s > self.acked} - self.delivered
        if fresh:
            self.delivered |= fresh
            self._save_delivered()

    def _save_delivered(self) -> None:
        self.store.meta_set("host_delivered", json.dumps(sorted(self.delivered)))

    def _ack(self) -> None:
        pending = self._buffered()
        upto = (min(pending) - 1) if pending else self.seen
        if upto > self.acked:
            self.acked = upto
            self.store.meta_set("host_seq", str(upto))
            self.link.ack(upto)
            before = len(self.delivered)
            self.delivered = {s for s in self.delivered if s > upto}
            if len(self.delivered) != before:
                self._save_delivered()

    # -- events ----------------------------------------------------------
    async def _on_welcome(self, w: dict[str, Any]) -> None:
        epoch = w.get("epoch")
        if epoch and epoch != self.epoch:
            self.epoch = epoch
            self.store.meta_set("host_epoch", epoch)
        for s in w.get("sessions") or []:
            rt = self._rt(s["name"])
            rt.state = s.get("state") or rt.state
            rt.info = s.get("info") or rt.info
            rt.limits = s.get("limits") or rt.limits
        # What is open now is the truth; a card for a request that is gone
        # is retired, one that is missing is sent.
        open_now = {r["rid"]: r for r in w.get("open_requests") or []}
        for rid in [r for r in self.requests if r not in open_now]:
            req = self.requests.pop(rid)
            await self._retire(req, _("⏹ Question closed"))
        for rid, r in open_now.items():
            if rid not in self.requests:
                self.requests[rid] = _Req(rid=rid, name=r["name"], tool=r["tool"],
                                          input=r.get("input") or {}, data=r)
                try:
                    await self._card(self.requests[rid])
                except Exception as exc:
                    log.exception("card for %s failed", rid[:8])
                    await self._report_failure(exc)
        if w.get("gap"):
            await self._say(_("⚠️ The bot was away longer than the session host "
                              "keeps its journal: some SDK output may be missing "
                              "from the chat."))

    async def _on_event(self, f: dict[str, Any]) -> None:
        seq = f.get("seq")
        if seq is not None and (seq <= self.acked or seq in self.delivered
                                or seq in self._buffered()):
            # Replayed, and already handled: it still counts as seen, or the
            # ack would stop short of it once the buffers empty.
            if seq in self.delivered:
                self.seen = max(self.seen, seq)
            return
        ev = f.get("ev") or ""
        with use_locale(self.watcher._locale()):
            try:
                await self._event(ev, f.get("name") or "", f.get("data") or {}, seq)
            except Exception as exc:
                # Never silent: said in the chat, and not acked as if it worked
                # only to block every later event behind it.
                log.exception("sdk event %s (seq %s) failed", ev, seq)
                await self._report_failure(exc)
        if seq is not None:
            self.seen = max(self.seen, seq)
            if seq not in self._buffered():
                self._deliver([seq])
        self._ack()

    async def _event(self, ev: str, name: str, d: dict[str, Any], seq: int | None) -> None:
        if ev == "host_restarted":
            await self._host_restarted(d)
            return
        rt = self._rt(name) if name else None
        if rt is None:
            return
        if ev == "text":
            self._buffer(rt, d.get("text") or "", seq)
            rt.turn_texts += 1
        elif ev == "tool":
            if d.get("name") not in _CARD_TOOLS:
                self._buffer(rt, "🔧 " + _summarise_tool(d), seq)
        elif ev == "prompt_taken":
            uid = str(d.get("uuid") or "")
            now = time.time()
            self.taken[uid] = now
            for old in [k for k, at in self.taken.items() if now - at > 600]:
                del self.taken[old]
            if self.on_taken is not None:
                await self.on_taken(name, uid)
        elif ev == "turn_start":
            rt.state = "busy"
            rt.work_since = rt.work_since or time.time()
            self._title_from(name, d.get("text") or "")
        elif ev == "turn_end":
            await self._turn_end(name, rt, d, seq)
        elif ev == "session_state":
            rt.state = d.get("state") or rt.state
            if rt.state in ("idle", "dormant", "closed"):
                rt.work_since = 0.0
        elif ev == "session_up":
            rt.info.update(d.get("info") or {})
            rt.state = "idle"
        elif ev == "request_open":
            await self._request_open(name, rt, d)
        elif ev == "request_closed":
            req = self.requests.pop(d.get("rid") or "", None)
            if req is not None and d.get("how") != "answered":
                await self._retire(req, _("⏹ Question closed"))
        elif ev == "session_reset":
            await self._session_reset(name, d)
        elif ev == "fault":
            await self._fault(name, rt, d)
        elif ev == "notice":
            await self._notice(name, rt, d, seq)
        elif ev == "metrics":
            rt.limits = d.get("limits") or rt.limits
            rt.context = d.get("context") or rt.context
            rt.metrics_at = time.time()
            if d.get("model"):
                rt.info["model"] = d["model"]
            if self._holding(name):
                rt.usage_due = True     # announced once the card is answered
            else:
                await self._check_usage(name, rt)
        elif ev == "background":
            rt.info["background"] = d.get("tasks") or []
        elif ev == "mode":
            rt.info["mode"] = d.get("mode") or rt.info.get("mode")
        elif ev == "effort":
            rt.info["effort"] = d.get("effort") or rt.info.get("effort")

    def _title_from(self, name: str, prompt: str) -> None:
        """A session with no name but its launch one is called by its first task.

        "pay4say-0e5e" is not something anyone remembers; what it was asked
        first is (the tmux watcher used to do this for every session).
        """
        mgd = self.store.by_name(name)
        if mgd is None or mgd.title or not mgd.is_auto_named:
            return
        title = " ".join(clean_prompt(prompt).split())[:60]
        if title and not title.startswith("/"):
            self.store.set_title(mgd.session_id, title)

    def _buffer(self, rt: _Rt, text: str, seq: int | None) -> None:
        if not text.strip():
            return
        rt.buffer.append(text)
        rt.buffer_since = rt.buffer_since or time.time()
        if seq is not None:
            rt.seqs.add(seq)

    async def _flush(self, name: str) -> None:
        rt = self._rt(name)
        if not rt.buffer:
            return
        body = "\n\n".join(rt.buffer).strip()
        mgd = self.store.by_name(name)
        if mgd is None:
            log.info("dropping output of an unknown sdk session %s", name)
        else:
            log.info("flush id=%s name=%s chars=%d", mgd.session_id[:8], name, len(body))
            await self._drop_pulse(rt)
            await self.watcher._emit(mgd.session_id, mgd.full_label, body,
                                     self.usage_for(mgd.session_id))
        self._deliver(rt.seqs)
        rt.buffer.clear()
        rt.seqs.clear()
        rt.buffer_since = 0.0

    async def _turn_end(self, name: str, rt: _Rt, d: dict[str, Any], seq: int | None) -> None:
        result = (d.get("result") or "").strip()
        if d.get("cost") is not None:
            rt.cost = float(d["cost"] or 0.0)
        if d.get("terminal_reason") in ("aborted_streaming", "aborted_tools"):
            self._buffer(rt, _("⏹ Interrupted."), seq)
        elif d.get("is_error"):
            detail = "; ".join(d.get("errors") or []) or result or d.get("subtype") or ""
            status = d.get("api_error_status")
            self._buffer(rt, _("⚠️ The turn ended with an error{status}: {detail}").format(
                status=f" (HTTP {status})" if status else "", detail=detail[:500]), seq)
        elif rt.turn_texts == 0 and result:
            # A local command (/cost, /context, /usage…) answers here only.
            self._buffer(rt, result, seq)
        rt.turn_texts = 0
        if not d.get("queued"):
            rt.state = "idle"
            rt.work_since = 0.0
        if not self._holding(name):
            await self._flush(name)

    async def _session_reset(self, name: str, d: dict[str, Any]) -> None:
        old, new = d.get("old_sid") or "", d.get("new_sid") or ""
        mgd = self.store.by_name(name)
        if mgd is None or not new or mgd.session_id == new:
            return
        log.info("sdk session rebound %s -> %s (name=%s)", old[:8], new[:8], name)
        self.store.rebind(mgd.session_id, new)
        self._refresh_ids()
        self._forget_context(self._rt(name))
        await self._say(_("🧹 <b>{name}</b>: context cleared — the session was given a "
                          "new id, and I am following it there.").format(
                              name=html.escape(mgd.full_label)),
                        new, parse_mode="HTML", reply_markup=menu_kb(new))

    async def _fault(self, name: str, rt: _Rt, d: dict[str, Any]) -> None:
        mgd = self.store.by_name(name)
        if mgd is None:
            return
        await self._flush(name)
        rt.state = "dormant"
        prompts = d.get("prompts") or []
        lines = [_("⚠️ <b>{name}</b>: the Claude process has ended.").format(
            name=html.escape(mgd.full_label)),
            f"<i>{html.escape(str(d.get('message') or '')[:600])}</i>", "",
            _("The session is resting: your next message resumes it with its context.")]
        if prompts:
            lines += ["", _("Cut off with it:")] + [
                f"• <code>{html.escape(p[:300])}</code>" for p in prompts]
        await self._say("\n".join(lines), mgd.session_id, parse_mode="HTML",
                        reply_markup=self.again_kb([(name, p) for p in prompts]))

    async def _notice(self, name: str, rt: _Rt, d: dict[str, Any], seq: int | None) -> None:
        if d.get("kind") == "compacted":
            self._forget_context(rt)
            self._buffer(rt, _("🗜 Context compacted."), seq)
        elif d.get("kind") == "assistant_error":
            self._buffer(rt, _("⚠️ Claude reported an error: {error}").format(
                error=d.get("error")), seq)

    async def _host_restarted(self, d: dict[str, Any]) -> None:
        interrupted = d.get("interrupted") or []
        lost = d.get("lost") or []
        if not interrupted and not lost:
            return
        lines = [_("🔌 <b>The session host was restarted.</b>"),
                 _("SDK sessions are resting and wake up with your next message.")]
        for item in interrupted:
            mgd = self.store.by_name(item.get("name") or "")
            label = mgd.full_label if mgd else item.get("name")
            lines += ["", _("✂️ <b>{name}</b> was cut off in the middle of:").format(
                name=html.escape(str(label)))]
            lines += [f"• <code>{html.escape(str(p)[:300])}</code>"
                      for p in item.get("prompts") or []]
        if lost:
            lines += ["", ngettext("❓ {count} open question was closed with it.",
                                   "❓ {count} open questions were closed with it.",
                                   len(lost)).format(count=len(lost))]
        cut = [(str(item.get("name") or ""), str(p)) for item in interrupted
               for p in item.get("prompts") or []]
        names = {n for n, _p in cut}
        only = self.store.by_name(next(iter(names))) if len(names) == 1 else None
        await self._say("\n".join(lines), only.session_id if only else None,
                        parse_mode="HTML", reply_markup=self.again_kb(cut))

    def again_kb(self, items: list[tuple[str, str]]) -> InlineKeyboardMarkup | None:
        """«🔁 Send again» for each prompt that was cut off (at most five)."""
        if not items:
            return None
        kb = InlineKeyboardBuilder()
        for name, text in items[:5]:
            n = self.again_next
            self.again_next += 1
            self.again[n] = (name, text)
            short = " ".join(text.split())
            kb.row(InlineKeyboardButton(
                text=_("🔁 Send again: {text}").format(text=short)[:60],
                callback_data=f"again:{n}"))
        self.save_again()
        return kb.as_markup()

    def save_again(self) -> None:
        """Keep the cut-off prompts across bot restarts, the newest fifty."""
        for n in sorted(self.again)[:-50]:
            del self.again[n]
        self.store.meta_set("sdk_again", json.dumps(
            {str(k): list(v) for k, v in self.again.items()}, ensure_ascii=False))
        self.store.meta_set("sdk_again_next", str(self.again_next))

    # -- cards -----------------------------------------------------------
    async def _request_open(self, name: str, rt: _Rt, d: dict[str, Any]) -> None:
        rid = d.get("rid") or ""
        req = _Req(rid=rid, name=name, tool=d.get("tool") or "?",
                   input=d.get("input") or {}, data=d, tried=time.monotonic())
        self.requests[rid] = req
        await self._flush(name)
        await self._drop_pulse(rt)
        await self._card(req)

    async def _card(self, req: _Req, fresh: bool = False) -> None:
        if req.sending:
            return
        req.sending = True
        try:
            await self._send_card(req, fresh)
        finally:
            req.sending = False

    async def _send_card(self, req: _Req, fresh: bool) -> None:
        req.tried = time.monotonic()
        mgd = self.store.by_name(req.name)
        if mgd is None:
            log.warning("request %s for an unknown sdk session %s", req.rid[:8], req.name)
            return
        known = None if fresh else self.store.card_of(req.rid)
        if known is not None:
            req.msg_id = known
            if req.kind == "question":
                # Adopted after a restart: the progress through its parts was
                # in the old process's memory, so the card starts again from
                # part one — rather than keep showing a part this one is not on.
                await self._redraw(req)
            return
        try:
            text, kb = self._render(req, mgd)
        except Exception:
            log.exception("card render failed for %s %s", req.tool, req.rid[:8])
            text, kb = self._render_fallback(req, mgd)
        msg = await rich.send(self.bot, self.chat_id, markdown=text, reply_markup=kb) \
            if len(text) <= rich.DOC_LIMIT else None
        if msg is None:
            chunks = split_text(_plain(text))
            for chunk in chunks[:-1]:
                await self._say(chunk, mgd.session_id)
            msg = await self._say(chunks[-1], mgd.session_id, reply_markup=kb)
        if msg is None:
            return
        self.store.remember_message(msg.message_id, mgd.session_id)
        req.msg_id = msg.message_id
        req.at = time.monotonic()
        self.store.remember_card(req.rid, mgd.session_id, msg.message_id)
        log.info("card %s id=%s tool=%s", req.kind, mgd.session_id[:8], req.tool)

    def _render(self, req: _Req, mgd: Managed) -> tuple[str, InlineKeyboardMarkup]:
        who = rich.md_escape(mgd.full_label)
        r8 = req.rid[:8]
        kb = InlineKeyboardBuilder()
        if req.kind == "question":
            return self._render_question(req, who, r8)
        if req.kind == "plan":
            plan = str(req.input.get("plan") or "")
            text = _("**📋 {name}: a plan for your approval**").format(name=who) \
                + "\n\n" + plan
            kb.row(InlineKeyboardButton(text=_("✅ Approve"), callback_data=f"rq:{r8}:pa"))
            kb.row(InlineKeyboardButton(text=_("✅ Approve, accept edits"),
                                        callback_data=f"rq:{r8}:pe"))
            kb.row(InlineKeyboardButton(text=_("✏️ Revise"), callback_data=f"rq:{r8}:pr"),
                   InlineKeyboardButton(text=_("❌ Keep planning"),
                                        callback_data=f"rq:{r8}:pk"))
            return text, kb.as_markup()
        d = req.data
        title = d.get("title") or _("Claude wants to use {tool}").format(tool=req.tool)
        parts = [f"**🔐 {who}: {rich.md_escape(str(title))}**"]
        cmd = req.input.get("command")
        if req.tool == "Bash" and isinstance(cmd, str):
            parts.append(f"```\n{cmd[:3000]}\n```")
            if req.input.get("description"):
                parts.append(f"_{rich.md_escape(str(req.input['description']))}_")
        elif isinstance(req.input.get("file_path"), str):
            parts.append(f"`{req.input['file_path']}`")
            body = req.input.get("new_string") or req.input.get("content")
            if isinstance(body, str) and body:
                parts.append(f"```\n{body[:2000]}\n```")
        else:
            parts.append("```\n" + json.dumps(req.input, ensure_ascii=False,
                                              indent=1)[:2500] + "\n```")
        if d.get("reason"):
            parts.append(f"_{rich.md_escape(str(d['reason']))}_")
        kb.row(InlineKeyboardButton(text=_("✅ Allow"), callback_data=f"rq:{r8}:a"),
               InlineKeyboardButton(text=_("❌ Deny"), callback_data=f"rq:{r8}:d"))
        kb.row(InlineKeyboardButton(text=_("✏️ Deny with a reason"),
                                    callback_data=f"rq:{r8}:r"))
        for i, s in enumerate((d.get("suggestions") or [])[:2]):
            label = _rule_label(s)
            if label:
                kb.row(InlineKeyboardButton(
                    text=_("✅ Always: {rule}").format(rule=label)[:60],
                    callback_data=f"rq:{r8}:A{i}"))
        return "\n\n".join(parts), kb.as_markup()

    def _render_fallback(self, req: _Req, mgd: Managed) -> tuple[str, InlineKeyboardMarkup]:
        """A card for a request whose data the renderer choked on: still answerable."""
        r8 = req.rid[:8]
        kb = InlineKeyboardBuilder()
        if req.kind == "question":
            kb.row(InlineKeyboardButton(text=_("🚫 Don't answer"), callback_data=f"rq:{r8}:x"))
        else:
            kb.row(InlineKeyboardButton(text=_("✅ Allow"), callback_data=f"rq:{r8}:a"),
                   InlineKeyboardButton(text=_("❌ Deny"), callback_data=f"rq:{r8}:d"))
        body = json.dumps(req.input, ensure_ascii=False, default=str)[:2500]
        return (f"**🔐 {rich.md_escape(mgd.full_label)}: {rich.md_escape(req.tool)}**"
                f"\n\n```\n{body}\n```"), kb.as_markup()

    def _render_question(self, req: _Req, who: str, r8: str) -> tuple[str, InlineKeyboardMarkup]:
        qs = req.questions
        kb = InlineKeyboardBuilder()
        if not qs:
            kb.row(InlineKeyboardButton(text=_("🚫 Don't answer"),
                                        callback_data=f"rq:{r8}:x"))
            return _("**❓ {name}** asks a question this bot cannot read.").format(
                name=who), kb.as_markup()
        q = qs[min(req.index, len(qs) - 1)]
        multi = bool(q.get("multiSelect"))
        head = f"**❓ {who}**"
        if q.get("header"):
            head += f" · {rich.md_escape(str(q['header']))}"
        if len(qs) > 1:
            head += f" ({req.index + 1}/{len(qs)})"
        lines = [head, "", str(q.get("question") or "")]
        opts = q.get("options") or []
        lines.append("")
        for i, o in enumerate(opts):
            desc = o.get("description")
            lines.append(f"{i + 1}. **{rich.md_escape(str(o.get('label')))}**"
                         + (f" — {desc}" if desc else ""))
        if multi:
            lines += ["", _("_Several can be ticked; then press {button}._").format(
                button=_("✅ Next") if req.index < len(qs) - 1 else _("✅ Send"))]
        for i, o in enumerate(opts):
            label = str(o.get("label") or i + 1)
            mark = ("☑️ " if i in req.ticks else "☐ ") if multi else f"{i + 1}. "
            kb.row(InlineKeyboardButton(text=(mark + label)[:60],
                                        callback_data=f"rq:{r8}:o{req.index}.{i}"))
        nav = []
        if req.index > 0:
            nav.append(InlineKeyboardButton(text=_("⬅️ Back"),
                                            callback_data=f"rq:{r8}:b{req.index}"))
        if multi:
            nav.append(InlineKeyboardButton(
                text=_("✅ Next") if req.index < len(qs) - 1 else _("✅ Send"),
                callback_data=f"rq:{r8}:n{req.index}"))
        if nav:
            kb.row(*nav)
        kb.row(InlineKeyboardButton(text=_("✏️ Own answer"),
                                    callback_data=f"rq:{r8}:t{req.index}"),
               InlineKeyboardButton(text=_("💬 Discuss"), callback_data=f"rq:{r8}:c"))
        kb.row(InlineKeyboardButton(text=_("🚫 Don't answer"), callback_data=f"rq:{r8}:x"))
        return "\n".join(lines), kb.as_markup()

    async def _retire(self, req: _Req, label: str) -> None:
        if req.msg_id is None:
            return
        with contextlib.suppress(Exception):
            await self.bot.edit_message_reply_markup(
                chat_id=self.chat_id, message_id=req.msg_id, reply_markup=done_kb(label))

    async def redraw(self, req: _Req) -> None:
        await self._redraw(req)

    async def _redraw(self, req: _Req) -> None:
        mgd = self.store.by_name(req.name)
        if mgd is None or req.msg_id is None:
            return
        text, kb = self._render(req, mgd)
        if await rich.edit(self.bot, self.chat_id, req.msg_id, markdown=text,
                           reply_markup=kb):
            return
        with contextlib.suppress(Exception):
            await self.bot.edit_message_text(_plain(text)[:4000], chat_id=self.chat_id,
                                             message_id=req.msg_id, reply_markup=kb)

    # -- answers from the chat -------------------------------------------
    def find(self, short: str) -> _Req | None:
        return next((q for r, q in self.requests.items() if r.startswith(short)), None)

    async def callback(self, c: CallbackQuery, arg: str) -> Managed | None:
        """A button of an SDK card (`rq:<rid8>:<action>`); the session, if answered."""
        short, _sep, act = arg.partition(":")
        req = self.find(short)
        if req is None:
            await c.answer(_("This question is no longer open"), show_alert=True)
            if c.message is not None and isinstance(c.message, Message):
                with contextlib.suppress(Exception):
                    await c.message.edit_reply_markup(reply_markup=done_kb(
                        _("⏹ Question closed")))
            return None
        mgd = self.store.by_name(req.name)
        if req.kind == "question" and act[:1] in ("o", "n", "b"):
            return await self._question_step(c, req, act, mgd)
        answer: dict[str, Any]
        if act == "a" or act == "pa":
            answer, label = {"behavior": "allow"}, _("✅ Allowed") if act == "a" \
                else _("✅ Plan approved")
        elif act == "pe":
            answer, label = ({"behavior": "allow", "set_mode": "acceptEdits"},
                             _("✅ Approved, edits accepted"))
        elif act.startswith("A") and act[1:].isdigit():
            answer = {"behavior": "allow", "suggestions": [int(act[1:])]}
            label = _("✅ Always allowed")
        elif act == "pk":
            answer = {"behavior": "deny", "message":
                      "The user wants to keep planning. Ask what to change, or refine the plan."}
            label = _("❌ Keep planning")
        elif act == "x":
            answer = {"behavior": "deny", "message": "The user chose not to answer."}
            label = _("🚫 Not answered")
        elif act == "c":
            # Answers nothing: Claude asks first, and the next message is read
            # as the reply to that (the TUI's "Chat about this").
            answer = {"behavior": "deny", "message":
                      "The user wants to talk about this question before answering. "
                      "Ask them what they would like to clarify, then wait for the reply."}
            label = _("💬 To be discussed — your next message goes there")
        else:
            answer = {"behavior": "deny", "message": "The user denied this in Telegram."}
            label = _("❌ Denied")
        ok = await self._answer(req, answer, label)
        await c.answer(label if ok else _("Could not pass the answer on — see /log"),
                       show_alert=not ok)
        return mgd if ok else None

    async def _question_step(self, c: CallbackQuery, req: _Req, act: str,
                             mgd: Managed | None) -> Managed | None:
        qs = req.questions
        # Every question button names the part it was drawn for; a tap from a
        # card that shows another part than the one in hand is not applied.
        kind, rest = act[0], act[1:]
        part_s, _dot, opt_s = rest.partition(".")
        if not part_s.isdigit() or int(part_s) != req.index or not qs:
            await c.answer(_("The card was out of date — it is shown again"))
            await self._redraw(req)
            return None
        act = kind + opt_s
        q = qs[req.index]
        opts = q.get("options") or []
        multi = bool(q.get("multiSelect"))
        if act == "b":
            req.index = max(0, req.index - 1)
            req.ticks = set()
            await c.answer()
            await self._redraw(req)
            return None
        if act.startswith("o"):
            i = int(act[1:]) if act[1:].isdigit() else -1
            if not 0 <= i < len(opts):
                await c.answer()
                return None
            if multi:
                req.ticks ^= {i}
                await c.answer()
                await self._redraw(req)
                return None
            req.answers[str(q.get("question"))] = opts[i].get("label")
        elif act == "n":
            req.answers[str(q.get("question"))] = [opts[i].get("label")
                                                  for i in sorted(req.ticks)]
        done, ok = await self._advance(req)
        if not done:
            await c.answer()
            return None
        await c.answer(_("Answered") if ok else _("Could not pass the answer on — see /log"),
                       show_alert=not ok)
        return mgd if ok else None

    async def _advance(self, req: _Req) -> tuple[bool, bool]:
        """After a part is answered: the next part, or the whole answer sent.

        (finished, delivered) — finished False means the card now shows the
        next part.
        """
        qs = req.questions
        req.ticks = set()
        if req.index < len(qs) - 1:
            req.index += 1
            await self._redraw(req)
            return False, False
        summary = "; ".join(v if isinstance(v, str) else ", ".join(v)
                            for v in req.answers.values())
        ok = await self._answer(req, {"behavior": "allow", "updated_input": {
            "questions": qs, "answers": req.answers}}, "✅ " + summary)
        return True, ok

    # -- answers typed in the chat ----------------------------------------
    def open_question(self, mgd: Managed) -> _Req | None:
        """The session's open AskUserQuestion, if it has one."""
        return next((q for q in self.requests.values()
                     if q.name == mgd.name and q.kind == "question"), None)

    def text_fits(self, short: str, act: str) -> bool:
        """Whether a ✏️ button still matches its request (and its part)."""
        req = self.find(short)
        if req is None:
            return False
        if act.startswith("t"):
            return req.kind == "question" and act[1:].isdigit() and int(act[1:]) == req.index
        return True

    async def answer_text(self, short: str, act: str,
                          text: str) -> tuple[str, Managed | None, str]:
        """A message typed after ✏️ on a card: (status, the session, what happened).

        *act* is the button that asked for it: "t<part>" — the text answers
        that part of a question; "pr" — changes the plan should get; "r" — why
        a request is denied. Status: "done" — delivered; "next" — a part taken,
        the card shows the next one; "failed" — not applied (the caller must
        not drop the text: the session, when known, is returned with it).
        """
        req = self.find(short)
        if req is None:
            return "failed", None, _("This question is no longer open.")
        mgd = self.store.by_name(req.name)
        text = text.strip()
        if act.startswith("t") and req.kind == "question":
            if not act[1:].isdigit() or int(act[1:]) != req.index or not req.questions:
                await self._redraw(req)
                return "failed", mgd, _("The question has moved on — its card is shown again.")
            q = req.questions[req.index]
            req.answers[str(q.get("question"))] = text
            done, ok = await self._advance(req)
            if not done:
                return "next", mgd, _("✏️ Answer taken — the next part is on the card.")
            return ("done", mgd, _("✏️ Answered.")) if ok else \
                ("failed", mgd, _("Could not pass the answer on — see /log"))
        if act == "pr":
            answer = {"behavior": "deny", "message":
                      f"The user wants the plan changed: {text}. Revise the plan and "
                      "present it again with ExitPlanMode."}
            label = _("✏️ Sent back with changes")
        else:
            answer = {"behavior": "deny", "message": f"The user denied this, saying: {text}"}
            label = _("❌ Denied with a reason")
        ok = await self._answer(req, answer, label)
        return ("done", mgd, label) if ok else \
            ("failed", mgd, _("Could not pass the answer on — see /log"))

    async def _answer(self, req: _Req, answer: dict[str, Any], label: str) -> bool:
        try:
            data = await self.link.call("answer", rid=req.rid, answer=answer)
        except HostError as exc:
            log.warning("answer %s not delivered: %s", req.rid[:8], exc)
            return False
        result = data.get("result")
        self.requests.pop(req.rid, None)
        if result == "ok":
            log.info("answered %s %s: %s", req.kind, req.rid[:8], label)
            await self._retire(req, label)
            return True
        await self._retire(req, _("⏹ Question closed") if result != "already"
                           else _("✅ Already answered"))
        return result == "already"

    # -- sending -----------------------------------------------------------
    async def _say(self, text: str, session_id: str | None = None, **kw) -> Message | None:
        try:
            msg = await self.bot.send_message(self.chat_id, text, **kw)
        except Exception:
            log.exception("send_message failed")
            return None
        if session_id:
            self.store.remember_message(msg.message_id, session_id)
        return msg


def _model_name(model_id: str) -> str:
    """"claude-opus-5-5" → "Opus 5.5", the way Claude Code names models."""
    base, _br, tail = model_id.partition("[")
    if tail:
        name = _model_name(base)
        return f"{name} [{tail}" if name != base else model_id
    parts = model_id.removeprefix("claude-").split("-")
    if len(parts) < 2 or not parts[0].isalpha():
        return model_id
    family, version = parts[0], [x for x in parts[1:] if x.isdigit() and len(x) < 3]
    return f"{family.capitalize()} {'.'.join(version)}".strip() if version else model_id


def _plain(markdown: str) -> str:
    """The fallback text of a card when rich messages are refused."""
    return markdown.replace("**", "").replace("```", "").replace("\\", "")


def _rule_label(s: dict[str, Any]) -> str:
    """What an "always allow" suggestion would add, as a short rule."""
    rules = s.get("rules") or []
    if s.get("type") == "addRules" and rules:
        r = rules[0]
        tool = r.get("toolName") or r.get("tool_name") or ""
        content = r.get("ruleContent") or r.get("rule_content")
        return f"{tool}({content})" if content else str(tool)
    if s.get("type") == "setMode":
        return str(s.get("mode") or "")
    if s.get("type") == "addDirectories":
        dirs = s.get("directories") or []
        return ", ".join(str(x) for x in dirs)[:40]
    return ""
