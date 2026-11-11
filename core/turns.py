"""Turn log: one entry per private-chat inbound message, with the skills loaded for it.

Hermes-free. State is any object with get(key, default) and set(key, value) (ctx.state in Hermes,
see hermes_cli/plugins_state.py:173-180 and plugins_context.py:88-92; MemoryState in tests).
See GLOSSARY.md: Turn, Message id range, Skill behind a reply.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("thumbs-fix")

MAX_TURNS_PER_CHAT = 200
MAX_BOUND_SESSIONS = 400
_TURNS_KEY = "turns"
_SESSIONS_KEY = "sessions"
_SENDERS_KEY = "senders"


class MemoryState:
    """In-memory stand-in for ctx.state (tests, or a ctx without state)."""

    def __init__(self) -> None:
        self._d: dict[str, Any] = {}

    def get(self, key: str, default: Any = None) -> Any:
        return json.loads(json.dumps(self._d[key])) if key in self._d else default

    def set(self, key: str, value: Any) -> None:
        self._d[key] = json.loads(json.dumps(value))


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _platform(x: Any) -> str:
    """Normalised platform name; the gateway may pass an enum, "telegram" or policy.platform."""
    return str(getattr(x, "value", x) or "").strip().lower()


def skill_view_name(args: Any, result: Any) -> str | None:
    """Name of the skill a skill_view call loaded, or None when the call failed.

    Hermes: success is {"success": true, "name": ...} (tools/skills_tool.py:698-699, also the
    "unchanged" stub in tools/skills_tool_dedup.py:39); failure is {"success": false, "error": ...}
    (tools/skills_tool_plugin.py:29-30) or {"error": ...} (tools/registry.py:1198-1201).
    """
    parsed = result
    if isinstance(result, (str, bytes)):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
    if not isinstance(parsed, dict) or parsed.get("success") is not True or parsed.get("error"):
        return None
    name = parsed.get("name") or (args.get("name") if isinstance(args, dict) else None)
    return str(name) if name else None


class TurnLog:
    def __init__(self, state: Any) -> None:
        self.state = state

    # -- inbound ---------------------------------------------------------------------------
    def record_inbound(self, event: Any) -> dict | None:
        """Record a private-chat Telegram inbound message as a turn. Anything else is ignored.

        Hermes: event.source.platform / chat_id / chat_type ("dm" is private) are on
        SessionSource (gateway/session.py:69-75), event.message_id is a str and event.internal marks
        synthetic events (gateway/platforms/event.py:78,115); the Telegram adapter builds them in
        plugins/platforms/telegram/adapter.py:6517-6540.
        """
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            log.debug("thumbs-fix turns: skip inbound (no source or internal)")
            return None
        if _platform(getattr(source, "platform", "")) != "telegram":
            log.debug("thumbs-fix turns: skip inbound (not telegram)")
            return None
        is_dm = getattr(source, "chat_type", "") == "dm"
        # Note where each sender's latest message came from, so pre_llm_call can tell a DM turn from a
        # group turn by the same user. The inbound hook runs before the agent turn it starts.
        user_id = str(getattr(source, "user_id", None) or getattr(event, "user_id", None) or "")
        if user_id:
            senders = self.state.get(_SENDERS_KEY, {})
            senders.pop(user_id, None)
            senders[user_id] = {"dm": is_dm}
            while len(senders) > MAX_BOUND_SESSIONS:
                senders.pop(next(iter(senders)))
            self.state.set(_SENDERS_KEY, senders)
        if not is_dm:
            log.debug("thumbs-fix turns: noted non-private inbound from sender=%s, no turn", user_id)
            return None
        chat_id = str(getattr(source, "chat_id", "") or "")
        message_id = _as_int(getattr(event, "message_id", None))
        if not chat_id or message_id is None:
            log.debug("thumbs-fix turns: skip inbound (no chat id or numeric message id)")
            return None
        turns = self.state.get(_TURNS_KEY, {})
        chat_turns = turns.setdefault(chat_id, [])
        if any(t["message_id"] == message_id for t in chat_turns):
            return None
        turn = {"chat_id": chat_id, "message_id": message_id, "skills": [], "session_id": ""}
        chat_turns.append(turn)
        chat_turns.sort(key=lambda t: t["message_id"])
        del chat_turns[:-MAX_TURNS_PER_CHAT]
        self.state.set(_TURNS_KEY, turns)
        log.debug("thumbs-fix turns: recorded chat=%s msg=%s", chat_id, message_id)
        return turn

    def mark_user_message(self, event: Any) -> None:
        """Flag an inbound message the plugin consumed (an answer or a skill-name reply) as a plain user
        message. It stays in the log as a turn with no skills, so a later reaction on an id after it can
        never fall back into the previous turn's range."""
        source = getattr(event, "source", None)
        chat_id = str(getattr(source, "chat_id", "") or "")
        message_id = _as_int(getattr(event, "message_id", None))
        turns = self.state.get(_TURNS_KEY, {})
        for t in turns.get(chat_id, []):
            if t["message_id"] == message_id:
                t["user_message"] = True
                self.state.set(_TURNS_KEY, turns)
                return

    # -- session binding -------------------------------------------------------------------
    def bind_session(self, session_id: str, platform: str, sender_id: str, task_id: str = "") -> bool:
        """pre_llm_call: bind session_id (and task_id) to the chat. Private chat: sender id is chat id.

        Hermes passes session_id, task_id, platform, sender_id (agent/turn_context.py:919-931).
        Binds only when the sender's latest inbound was a Telegram DM (see record_inbound); otherwise any
        stale binding for the session/task is dropped. platform is compared normalised because the gateway
        passes policy.platform when a session policy exists (gateway/run_turn_runner.py:1300).
        """
        chat_id = str(sender_id or "")
        if not session_id or not chat_id:
            return False
        sessions = self.state.get(_SESSIONS_KEY, {})
        turns = self.state.get(_TURNS_KEY, {})
        latest = self.state.get(_SENDERS_KEY, {}).get(chat_id)
        if _platform(platform) != "telegram" or not latest or latest.get("dm") is not True or not turns.get(chat_id):
            stale = [k for k in (session_id, task_id) if k and sessions.pop(k, None) is not None]
            if stale:
                self.state.set(_SESSIONS_KEY, sessions)
            log.debug("thumbs-fix turns: not a private turn for sender=%s, session=%s unbound", chat_id, session_id)
            return False
        for key in (session_id, task_id):
            if key:
                sessions.pop(key, None)
                sessions[key] = chat_id
        while len(sessions) > MAX_BOUND_SESSIONS:
            sessions.pop(next(iter(sessions)))
        self.state.set(_SESSIONS_KEY, sessions)
        turns[chat_id][-1]["session_id"] = session_id
        self.state.set(_TURNS_KEY, turns)
        log.debug("thumbs-fix turns: bound session=%s task=%s to chat=%s", session_id, task_id, chat_id)
        return True

    # -- skill_view ------------------------------------------------------------------------
    def add_skill(self, *, session_id: str = "", task_id: str = "", args: Any = None, result: Any = None) -> bool:
        """post_tool_call for skill_view: add the skill to the session's current turn if it succeeded.

        Hermes supplies task_id and session_id, "" when absent (model_tools.py:664-676, 715-716);
        either may be the only one set, so session_id is tried first, then task_id.
        """
        name = skill_view_name(args, result)
        if name is None:
            log.debug("thumbs-fix turns: skill_view failed or unparsable, nothing added")
            return False
        sessions = self.state.get(_SESSIONS_KEY, {})
        chat_id = next((sessions[k] for k in (session_id, task_id) if k and k in sessions), None)
        if chat_id is None:
            log.debug("thumbs-fix turns: skill %s for unbound session=%s task=%s", name, session_id, task_id)
            return False
        turns = self.state.get(_TURNS_KEY, {})
        chat_turns = turns.get(chat_id)
        if not chat_turns:
            return False
        if name not in chat_turns[-1]["skills"]:
            chat_turns[-1]["skills"].append(name)
            self.state.set(_TURNS_KEY, turns)
        log.debug("thumbs-fix turns: skill %s added to chat=%s msg=%s", name, chat_id, chat_turns[-1]["message_id"])
        return True

    # -- lookup ----------------------------------------------------------------------------
    def turn_containing(self, chat_id: str, message_id: Any) -> dict | None:
        """The turn whose message id range holds message_id: the latest inbound id below it."""
        mid = _as_int(message_id)
        if mid is None:
            return None
        before = [t for t in self.state.get(_TURNS_KEY, {}).get(str(chat_id), []) if t["message_id"] < mid]
        return before[-1] if before else None

    def turns(self, chat_id: str) -> list[dict]:
        return list(self.state.get(_TURNS_KEY, {}).get(str(chat_id), []))
