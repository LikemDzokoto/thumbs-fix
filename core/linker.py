"""Reaction linker: a thumbs-down becomes a question naming the skill, or a refusal, or nothing.

Hermes-free and synchronous: route() decides, the hook adapter sends. See ADR 0001 (id range) and
GLOSSARY.md: Turn, Message id range, Refusal.

Payload (gateway_platform_event, Telegram reaction; plugins/platforms/telegram/adapter.py:2692-2718):
platform="telegram", event_type="reaction", payload={emojis, custom_emoji_ids, chat_id, message_id,
thread_id}. A removed reaction arrives with emojis == [] (new_reaction empty). The payload does not say who
reacted; the gateway has already authorised the reactor (gateway/run_adapters.py:1684).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .repairs import FAILED, IllegalTransition, RepairStore, StateError
from .turns import TurnLog

log = logging.getLogger("thumbs-fix")

THUMBS_DOWN = "\U0001F44E"
REFUSAL = "I can't tell which skill made this, nothing changed."
NO_SKILL = "No skill was behind this reply, nothing changed."
BUSY = "A repair is already waiting for your answer. Please answer it first."
NEED_TEXT = "Please send a short text answer. What was wrong?"
TRUNCATED = "Your answer was longer than 4096 characters, so I kept the first 4096."
MAX_QUESTION = 300


@dataclass
class Outcome:
    """What to do with a reaction. text is None when nothing should be sent."""
    action: str  # reactions: "ignore" | "ask" | "refuse" | "busy"; inbound: "pass" | "skip"
    chat_id: str = ""
    text: str | None = None
    repair_id: int | None = None
    result: dict | None = None  # inbound: the JSON-able handler result

    def json(self) -> str:
        return json.dumps(self.result if self.result is not None else {"ok": True, "action": self.action})


def _clean_emoji(e: Any) -> str:
    return str(e).replace("\ufe0f", "").strip()


def choice_for(skills: list[str]) -> str:
    """Lists the skills behind a reply and asks for one name (state awaiting-skill)."""
    names = ", ".join(f"`{_name(s)}`" for s in skills)
    return f"This reply used {len(skills)} skills: {names}. Which one should I fix? Reply with its name."


def _name(skill: Any) -> str:
    return "".join(c for c in str(skill) if c not in "`?\n\r").strip()[:120] or "unknown"


def _is_command(event: Any) -> bool:
    kind = getattr(event, "message_type", None)  # MessageType.COMMAND = "command" (gateway/platforms/event.py:56)
    if str(getattr(kind, "value", kind)).lower() == "command":
        return True
    fn = getattr(event, "is_command", None)  # MessageEvent.is_command (gateway/platforms/event.py:189)
    if callable(fn):
        return bool(fn())
    return str(getattr(event, "text", "") or "").lstrip().startswith("/")


def question_for(skill: str) -> str:
    """'This came from the skill `name`. What was wrong?': plain text, one question mark, <= 300 chars."""
    name = "".join(c for c in str(skill) if c not in "`?\n\r").strip()[:120] or "unknown"
    return f"This came from the skill `{name}`. What was wrong?"


class Linker:
    def __init__(self, turns: TurnLog, repairs: RepairStore, on_drafting: Callable[[dict], None] | None = None) -> None:
        self.turns = turns
        self.repairs = repairs
        self.on_drafting = on_drafting or (lambda repair: None)  # the seam ticket 6 fills

    def route(self, platform: Any, event_type: Any, payload: Any) -> Outcome:
        try:
            return self._route(platform, event_type, payload)
        except StateError as exc:  # corrupt state is left alone; nothing is sent or changed
            log.warning("thumbs-fix linker: %s", exc)
            return Outcome("ignore")

    def _route(self, platform: Any, event_type: Any, payload: Any) -> Outcome:
        ignore = Outcome("ignore")
        if _clean_emoji(getattr(platform, "value", platform)).lower() != "telegram" or event_type != "reaction":
            return ignore
        if not isinstance(payload, dict):
            return Outcome("ignore", result={"ok": False, "error": "reaction payload is not a mapping"})
        emojis = payload.get("emojis") or []
        if not isinstance(emojis, (list, tuple)) or THUMBS_DOWN not in {_clean_emoji(e) for e in emojis}:
            return ignore  # other emoji, or a removal (empty list)
        chat_id = str(payload.get("chat_id") or "").strip()
        try:
            mid = int(str(payload.get("message_id")))
        except (TypeError, ValueError):
            return Outcome("ignore", result={"ok": False, "error": "reaction has no usable message id"})
        # Telegram group, supergroup and channel ids are negative; a private chat id is the user id.
        if not chat_id or chat_id.startswith("-"):
            log.debug("thumbs-fix linker: ignoring reaction outside a private chat (chat=%s)", chat_id)
            return ignore
        turns = self.turns.turns(chat_id)
        if mid in {t["message_id"] for t in turns} or self.repairs.is_own(chat_id, mid):
            log.debug("thumbs-fix linker: reaction on a user or own message, chat=%s msg=%s", chat_id, mid)
            return ignore
        turn = self.turns.turn_containing(chat_id, mid)
        if turn is None:
            log.debug("thumbs-fix linker: refusing chat=%s msg=%s, no turn", chat_id, mid)
            return Outcome("refuse", chat_id, REFUSAL)
        if not turn["skills"]:
            return Outcome("refuse", chat_id, NO_SKILL)
        if self.repairs.open_for_turn(chat_id, turn["message_id"]) is not None:
            return ignore  # a second thumbs-down on the same turn: one repair only
        if self.repairs.waiting_for_text(chat_id) is not None:
            return Outcome("busy", chat_id, BUSY)
        repair = self.repairs.create(chat_id, turn["message_id"], turn["skills"])
        text = question_for(repair["skill"]) if repair["skill"] else choice_for(repair["candidates"])
        return Outcome("ask", chat_id, text, repair["id"])

    def inbound(self, event: Any) -> Outcome:
        """pre_gateway_dispatch: the next text message of a chat with a repair waiting is the answer.

        "skip" means the gateway must drop the message (no agent turn); "pass" means carry on.
        """
        keep = Outcome("pass")
        source = getattr(event, "source", None)
        if source is None or getattr(event, "internal", False):
            return keep
        if str(getattr(getattr(source, "platform", ""), "value", getattr(source, "platform", ""))).lower() != "telegram":
            return keep
        if getattr(source, "chat_type", "") != "dm":
            return keep
        chat_id = str(getattr(source, "chat_id", "") or "")
        repair = self.repairs.waiting_for_text(chat_id) if chat_id else None
        if repair is None or _is_command(event):
            return keep  # a slash command is never an answer
        kind = str(getattr(getattr(event, "message_type", "text"), "value", getattr(event, "message_type", "text")))
        text = getattr(event, "text", None)
        if kind != "text" or not isinstance(text, str) or not text.strip():
            err = "sticker, photo or empty message is not an answer" if kind != "text" else "empty answer"
            return Outcome("skip", chat_id, NEED_TEXT, result={"ok": False, "error": err, "state": repair["state"]})
        if repair["state"] == "awaiting-skill":
            wanted = text.strip().strip("`").strip().lower()
            skill = next((c for c in repair["candidates"] if c.lower() == wanted), None)
            if skill is None:
                again = f"I did not recognise that. {choice_for(repair['candidates'])}"
                return Outcome("skip", chat_id, again, result={"ok": False, "error": "unknown skill name", "state": repair["state"]})
            self.repairs.choose_skill(repair["id"], skill)
            return Outcome("skip", chat_id, question_for(skill), repair["id"], {"ok": True, "state": "awaiting-answer"})
        done = self.repairs.store_answer(repair["id"], text)
        try:
            self.on_drafting(done)
        except Exception:  # noqa: BLE001 - the seam must not undo a stored answer
            log.exception("thumbs-fix: drafting hook failed for repair %s", done["id"])
        note = TRUNCATED if done["feedback_truncated"] else None
        return Outcome("skip", chat_id, note, result={"ok": True, "repair": done["id"], "state": "drafting"})

    def sent(self, outcome: Outcome, message_ids: list[str]) -> None:
        """Record the plugin's own message ids; the question's id goes on its repair."""
        for m in message_ids:
            self.repairs.add_own(outcome.chat_id, m)
        if outcome.repair_id is not None and message_ids:
            self.repairs.update(outcome.repair_id, question_message_id=str(message_ids[-1]))

    def send_failed(self, outcome: Outcome) -> None:
        if outcome.repair_id is not None:
            try:
                self.repairs.transition(outcome.repair_id, FAILED)
            except (IllegalTransition, StateError):
                log.warning("thumbs-fix: could not mark repair %s failed", outcome.repair_id)
