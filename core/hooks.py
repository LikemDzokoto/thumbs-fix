"""Core entry points the hook adapters call. Later tickets fill in the rest.

Each takes plain keyword data and returns plain data; none imports Hermes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import applying, commands, drafting
from .linker import THUMBS_DOWN, Linker, Outcome, _clean_emoji
from .repairs import RepairStore, StateError
from .turns import MemoryState, TurnLog

log = logging.getLogger("thumbs-fix")

_turn_log: TurnLog = TurnLog(MemoryState())
_linker: Linker = Linker(_turn_log, RepairStore(MemoryState()), lambda r: drafting_hook(r))
_gateway: Any = None
_loop: Any = None
_llm: Any = None       # ctx.llm (agent/plugin_llm.py PluginLlm), or None
_dispatch: Any = None  # ctx.dispatch_tool (hermes_cli/plugins_context.py:483), or None
_data_dir: Any = None  # ctx.state.data_dir (hermes_cli/plugins_state.py:140), or None
THUMBS_UP = "\U0001F44D"
_tasks: set = set()  # strong refs so a scheduled send is not garbage-collected mid-flight


def drafting_hook(repair: dict) -> None:
    """Called once the answer is stored and the repair is `drafting`: say so, draft, send the diff (ticket 6)."""
    _spawn(_drafter.run(repair), None)


def _read_skill(name: str) -> str:
    return _view_skill(name)["content"]


def _view_skill(name: str) -> dict:
    """The skill's current skill_view result, through Hermes: ctx.dispatch_tool("skill_view", {"name": ...}) returns
    JSON {"success": true, "content": "<SKILL.md, preprocessed>", "skill_dir": "<abs dir>", ...}
    (tools/skills_tool.py:613-700, 733-785)."""
    if _dispatch is None:
        raise RuntimeError("ctx.dispatch_tool is unavailable")
    data = json.loads(_dispatch("skill_view", {"name": name}))
    if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("content"), str):
        raise ValueError(f"skill_view failed: {str((data or {}).get('error', 'no content'))[:200] if isinstance(data, dict) else 'bad reply'}")
    return data


def _complete(**kwargs: Any) -> Any:
    """ctx.llm.complete_structured(...): synchronous, so it runs in an executor (agent/plugin_llm.py:500-514)."""
    if _llm is None:
        raise RuntimeError("ctx.llm is unavailable")
    return _llm.complete_structured(**kwargs)


async def _run_blocking(fn: Any) -> Any:
    """Run a blocking call off the event loop; give up on it after the model timeout plus a grace period."""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(loop.run_in_executor(None, fn), drafting.TIMEOUT_S + 5)


async def _send_text(chat_id: str, text: str) -> list[str]:
    """Send one message through the gateway's Telegram adapter; the ids it landed as. Raises when it cannot."""
    adapter = _adapter_for("telegram")
    if adapter is None:
        raise RuntimeError("no Telegram adapter yet (no inbound message since start)")
    result = await adapter.send(chat_id, text)
    if not getattr(result, "success", False):
        raise RuntimeError(f"send failed: {getattr(result, 'error', 'unknown')}")
    return _sent_ids(result)


def _sent_ids(result: Any) -> list[str]:
    """Every id a send landed as, in order, without duplicates. A split send reports its extra parts in
    continuation_message_ids and, on Telegram success, all parts in raw_response["message_ids"]
    (plugins/platforms/telegram/adapter.py:3505, gateway/platforms/base.py:1591-1603)."""
    raw = getattr(result, "raw_response", None)
    raw_ids = raw.get("message_ids") if isinstance(raw, dict) else None
    ids = [*(raw_ids if isinstance(raw_ids, (list, tuple)) else ()), *(getattr(result, "continuation_message_ids", None) or ()),
           getattr(result, "message_id", None)]
    return list(dict.fromkeys(str(i) for i in ids if i))


def _make_drafter(repairs: RepairStore) -> drafting.Drafter:
    return drafting.Drafter(repairs, lambda n: _read_skill(n), lambda **kw: _complete(**kw),
                            lambda c, t: _send_text(c, t), lambda fn: _run_blocking(fn))


_drafter: drafting.Drafter = _make_drafter(_linker.repairs)


def _make_applier() -> applying.Applier:
    return applying.Applier(_linker.repairs, _turn_log, lambda n: _view_skill(n),
                            lambda tool, args: _dispatch_tool(tool, args), lambda: _data_dir)


def _dispatch_tool(tool: str, args: dict) -> str:
    if _dispatch is None:
        raise RuntimeError("ctx.dispatch_tool is unavailable")
    return _dispatch(tool, args)


_applier: applying.Applier = _make_applier()


def _make_commands() -> commands.Commands:
    return commands.Commands(_linker.repairs, _applier, lambda r: _start_retry(r), lambda t, a: _dispatch_tool(t, a),
                             lambda n: _view_skill(n))


_commands: commands.Commands = _make_commands()


def configure(state: Any, llm: Any = None, dispatch_tool: Any = None, data_dir: Any = None) -> None:
    """Called once from register(ctx) with ctx.state, ctx.llm and ctx.dispatch_tool.
    Falls back to memory when there is no state."""
    global _turn_log, _gateway, _linker, _loop, _llm, _dispatch, _drafter, _applier, _data_dir, _commands
    if state is None:
        log.warning("thumbs-fix: ctx.state is unavailable; turns are kept in memory only")
        state = MemoryState()
    _turn_log = TurnLog(state)
    _linker = Linker(_turn_log, RepairStore(state), lambda r: drafting_hook(r))
    _gateway = None
    _loop = None
    _llm, _dispatch = llm, dispatch_tool
    _data_dir = data_dir if data_dir is not None else getattr(state, "data_dir", None)
    _drafter = _make_drafter(_linker.repairs)
    _applier = _make_applier()
    _commands = _make_commands()


def get_turn_log() -> TurnLog:
    return _turn_log


def get_gateway() -> Any:
    """The gateway handle kept from the latest pre_gateway_dispatch (ADR 0002), or None."""
    return _gateway


def on_inbound(**kwargs: Any) -> dict | None:
    """pre_gateway_dispatch(event, gateway, session_store): record the turn, keep the gateway handle.

    Hermes calls it with exactly those kwargs (gateway/run_inbound_hooks.py:91-94). Returns None so the
    gateway carries on, or {"action": "skip"} when the message was the answer to a waiting repair: the
    gateway then drops it and starts no agent turn (gateway/run_inbound_hooks.py:93-101, run_inbound.py:221).
    """
    global _gateway, _loop
    if kwargs.get("gateway") is not None:
        _gateway = kwargs["gateway"]
        try:  # pre_gateway_dispatch runs on the gateway's loop (ainvoke_hook), so remember it
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            pass
    event = kwargs.get("event")
    _turn_log.record_inbound(event)
    try:
        out = _linker.inbound(event)
    except StateError as exc:  # corrupt state: leave it, let the message through
        log.warning("thumbs-fix: %s", exc)
        return None
    if out.action != "skip":
        return None
    _turn_log.mark_user_message(event)
    if out.text:
        _schedule(out)
    return {"action": "skip", "reason": "thumbs-fix: " + ("answer" if out.result and out.result.get("ok") else "needs a text answer")}


def on_pre_llm(**kwargs: Any) -> dict | None:
    """pre_llm_call: bind session to chat, then tell the agent once which skill was just changed.

    Returning {"context": "..."} adds it to the user message of this turn only (agent/turn_context.py:947-962),
    so the note fires once and the agent re-reads the skill instead of trusting its history."""
    sender = str(kwargs.get("sender_id") or "")
    _turn_log.bind_session(
        str(kwargs.get("session_id") or ""),
        kwargs.get("platform") or "",
        sender,
        str(kwargs.get("task_id") or ""),
    )
    try:
        note = _applier.take_note(sender)
    except Exception as exc:  # noqa: BLE001 - never break the agent's turn
        log.warning("thumbs-fix: could not read the skill-changed note: %s", exc)
        return None
    return {"context": note} if note else None


def log_tool_call(**kwargs: Any) -> dict:
    """Core of post_tool_call: log a successful skill_view on the session's current turn.
    Returns {"ok": true, "logged": bool} or {"ok": false, "error": ...}; never raises."""
    try:
        if kwargs.get("tool_name") != "skill_view":
            return {"ok": True, "logged": False}
        added = _turn_log.add_skill(
            session_id=str(kwargs.get("session_id") or ""),
            task_id=str(kwargs.get("task_id") or ""),
            args=kwargs.get("args"),
            result=kwargs.get("result"),
        )
        if not added:
            return {"ok": False, "error": "that skill_view call was not logged (it failed, or its chat is unknown)"}
        return {"ok": True, "logged": True}
    except Exception as exc:  # noqa: BLE001 - logging a skill must never break the agent's tool call
        log.warning("thumbs-fix: could not log a tool call: %s", exc)
        return {"ok": False, "error": f"could not log the tool call: {exc.__class__.__name__}"}


def on_tool_call(**kwargs: Any) -> None:
    """post_tool_call: log successful skill_view calls on the session's current turn. Returns None."""
    log_tool_call(**kwargs)
    return None


def _adapter_for(platform: str) -> Any:
    adapters = getattr(_gateway, "adapters", None) or {}
    for key, adapter in adapters.items():
        if str(getattr(key, "value", key)).lower() == platform:
            return adapter
    return None


async def _send(outcome: Outcome) -> None:
    """Send outcome.text through the gateway's Telegram adapter and record the ids it returns.

    adapter.send(chat_id, content, reply_to=None, metadata=None) -> SendResult(success, message_id, error,
    continuation_message_ids) (plugins/platforms/telegram/adapter.py:3403, gateway/platforms/base.py:1591).
    """
    try:
        adapter = _adapter_for("telegram")
        if adapter is None:
            log.warning("thumbs-fix: no Telegram adapter yet (no inbound message since start); cannot reply")
            _linker.send_failed(outcome)
            return
        result = await adapter.send(outcome.chat_id, outcome.text)
        if not getattr(result, "success", False):
            log.warning("thumbs-fix: send failed: %s", getattr(result, "error", "unknown"))
            _linker.send_failed(outcome)
            return
        _linker.sent(outcome, _sent_ids(result))
    except Exception:  # noqa: BLE001 - runs as a task; nobody awaits it
        log.exception("thumbs-fix: sending the reply failed")
        _linker.send_failed(outcome)


def _spawn(coro: Any, on_no_loop: Any) -> None:
    """Run a coroutine on the gateway's event loop from a synchronous hook.

    Hermes invokes gateway_platform_event inline from `async def _handle_gateway_platform_event`
    (gateway/run_adapters.py:1679-1685); the hook is not in the timeout-bounded set
    (hermes_cli/plugins_dispatch.py:42-60), so the callback runs on the loop thread and
    create_task is right. If it ever runs on another thread, fall back to the loop remembered from
    pre_gateway_dispatch with run_coroutine_threadsafe.
    """
    try:
        task = asyncio.get_running_loop().create_task(coro)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return
    except RuntimeError:
        pass
    if _loop is not None and _loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, _loop)
        return
    coro.close()
    log.warning("thumbs-fix: no running event loop to run on")
    if on_no_loop is not None:
        on_no_loop()


def _schedule(outcome: Outcome) -> None:
    _spawn(_send(outcome), lambda: _linker.send_failed(outcome))


def _decision(platform: Any, event_type: Any, payload: Any) -> tuple[str, dict] | None:
    """A thumbs-up or thumbs-down on one of a repair's own messages: ("approve"|"reject", repair), else None."""
    if _clean_emoji(getattr(platform, "value", platform)).lower() != "telegram" or event_type != "reaction":
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("emojis"), (list, tuple)):
        return None
    emojis = {_clean_emoji(e) for e in payload["emojis"]}
    verdict = "approve" if THUMBS_UP in emojis else "reject" if THUMBS_DOWN in emojis else None
    chat = str(payload.get("chat_id") or "").strip()
    if verdict is None or not chat or chat.startswith("-"):
        return None
    try:
        repair = _applier.for_message(chat, int(str(payload.get("message_id"))))
    except (TypeError, ValueError, StateError):
        return None
    return (verdict, repair) if repair is not None else None


async def _decide(verdict: str, repair: dict) -> None:
    """Approve or reject, then say what happened. Applying is blocking, so it runs off the event loop."""
    chat = repair["chat_id"]
    try:
        if verdict == "approve":
            result = await asyncio.get_running_loop().run_in_executor(None, lambda: _applier.approve(repair, "thumbs-up"))
        else:
            result = _applier.reject(repair)
        ids = await _send_text(chat, result["text"])
        for i in ids:
            _linker.repairs.add_own(chat, i)
    except Exception:  # noqa: BLE001 - runs as a task; nobody awaits it
        log.exception("thumbs-fix: handling a reaction on repair %s failed", repair.get("id"))


def on_platform_event(**kwargs: Any) -> None:
    """gateway_platform_event(platform, event_type, payload): a thumbs-up on a repair's diff applies it, a
    thumbs-down on the diff rejects it (a thumbs-up with no diff says so); other thumbs-downs go to the linker."""
    try:
        decision = _decision(kwargs.get("platform"), kwargs.get("event_type"), kwargs.get("payload"))
    except Exception:  # noqa: BLE001
        decision = None
    if decision is not None:
        verdict, repair = decision
        if verdict == "reject" and repair["state"] != "awaiting-approval":
            return None  # a thumbs-down on the question is not a rejection
        _spawn(_decide(verdict, repair), None)
        return None
    outcome = _linker.route(kwargs.get("platform"), kwargs.get("event_type"), kwargs.get("payload"))
    if outcome.text:
        _schedule(outcome)
    return None


def _start_retry(repair: dict) -> None:
    _spawn(_drafter.run(repair, announce=True), None)


def feedback_command(raw_args: str = "", source: Any = None, **_: Any) -> str:
    """/feedback handler: plain text, never raises. All the logic is in core/commands.py.

    Hermes passes `source` (the chat the command came from) to handlers that name it
    (hermes_cli/plugin_command_dispatch.py), so the command only touches that chat's repairs."""
    try:
        chat = str(getattr(source, "chat_id", "") or "") or None
        return _commands.handle(raw_args, chat)
    except Exception:  # noqa: BLE001
        log.exception("thumbs-fix: /feedback failed")
        return "Something went wrong, so nothing was changed."
