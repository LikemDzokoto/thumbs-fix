"""Repairs: the state machine, the durable queue in ctx.state, and own-message ids.

Hermes-free; state is any get/set store (ctx.state in Hermes). See GLOSSARY.md: Repair, Own message and
ARCHITECTURE.md (Data model). The state is validated on every read: anything unreadable or of the wrong
shape raises StateError and is never written over, so corrupt data stays where it is for the user to see.

States and moves (the only ones that exist):
  awaiting-skill   -> awaiting-answer | rejected | failed      (two skills: the user names one)
  awaiting-answer  -> drafting | rejected | failed             (the user's text answer is stored)
  drafting         -> awaiting-approval | rejected | failed    (ticket 6 drafts the edit)
  awaiting-approval-> applied | rejected | failed
  applied, rejected, failed are final.
`applied` is reachable only through mark_applied() with an explicit Approval; transition() refuses it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_REPAIRS = 100
MAX_OWN_MESSAGES_PER_CHAT = 400
MAX_ANSWER = 4096  # Telegram's message limit
_REPAIRS_KEY = "repairs"
_OWN_KEY = "own_messages"

AWAITING_SKILL = "awaiting-skill"
AWAITING_ANSWER = "awaiting-answer"
DRAFTING = "drafting"
AWAITING_APPROVAL = "awaiting-approval"
APPLIED = "applied"
REJECTED = "rejected"
FAILED = "failed"

STATES = (AWAITING_SKILL, AWAITING_ANSWER, DRAFTING, AWAITING_APPROVAL, APPLIED, REJECTED, FAILED)
_OPEN = (AWAITING_SKILL, AWAITING_ANSWER, DRAFTING, AWAITING_APPROVAL)
_WAITING_FOR_TEXT = (AWAITING_SKILL, AWAITING_ANSWER)
TRANSITIONS: dict[str, tuple[str, ...]] = {
    AWAITING_SKILL: (AWAITING_ANSWER, REJECTED, FAILED),
    AWAITING_ANSWER: (DRAFTING, REJECTED, FAILED),
    DRAFTING: (AWAITING_APPROVAL, REJECTED, FAILED),
    AWAITING_APPROVAL: (APPLIED, REJECTED, FAILED),
    APPLIED: (), REJECTED: (), FAILED: (),
}
APPROVAL_SOURCES = ("thumbs-up", "feedback-approve")


class StateError(Exception):
    """The plugin state is missing, unreadable or corrupt. Callers return a JSON error and write nothing."""


class IllegalTransition(Exception):
    """A move the state machine does not have."""


@dataclass(frozen=True)
class Approval:
    """An explicit approval event: a thumbs-up on the diff message, or `/feedback approve`."""
    source: str


def cut_answer(text: str) -> tuple[str, bool]:
    """The answer cut at 4096 characters (not bytes: emoji and non-Latin text are kept whole) and whether it was cut."""
    return (text[:MAX_ANSWER], True) if len(text) > MAX_ANSWER else (text, False)


class RepairStore:
    def __init__(self, state: Any) -> None:
        self.state = state

    # -- storage ---------------------------------------------------------------------------
    def _read(self, key: str, default: Any) -> Any:
        try:
            return self.state.get(key, default)
        except Exception as exc:  # noqa: BLE001 - ctx.state raises RuntimeError on an unparsable file
            raise StateError(f"cannot read {key}: {exc}") from exc

    def _write(self, key: str, value: Any) -> None:
        try:
            self.state.set(key, value)
        except Exception as exc:  # noqa: BLE001
            raise StateError(f"cannot write {key}: {exc}") from exc

    def _load(self) -> dict:
        data = self._read(_REPAIRS_KEY, None)
        if data is None:
            return {"next_id": 1, "items": []}
        ok = (
            isinstance(data, dict) and isinstance(data.get("next_id"), int) and isinstance(data.get("items"), list)
            and all(
                isinstance(r, dict) and isinstance(r.get("id"), int) and isinstance(r.get("chat_id"), str)
                and isinstance(r.get("turn"), int) and r.get("state") in STATES
                for r in data["items"]
            )
        )
        if not ok:
            raise StateError("repairs in plugin state are corrupt")
        return data

    # -- repairs ---------------------------------------------------------------------------
    def create(self, chat_id: str, turn_message_id: int, skills: list[str]) -> dict:
        """One skill: awaiting-answer. Several: awaiting-skill, until the user names one."""
        data = self._load()
        single = len(skills) == 1
        repair = {
            "id": data["next_id"], "chat_id": str(chat_id), "turn": int(turn_message_id),
            "skill": skills[0] if single else None, "candidates": list(skills),
            "state": AWAITING_ANSWER if single else AWAITING_SKILL,
            "feedback": None, "feedback_truncated": False, "question_message_id": None,
        }
        data["next_id"] += 1
        data["items"].append(repair)
        del data["items"][:-MAX_REPAIRS]
        self._write(_REPAIRS_KEY, data)
        return repair

    def all(self) -> list[dict]:
        return list(self._load()["items"])

    def get(self, repair_id: int) -> dict | None:
        return next((r for r in self.all() if r["id"] == repair_id), None)

    def open_repairs(self) -> list[dict]:
        return [r for r in self.all() if r["state"] in _OPEN]

    def open_for_turn(self, chat_id: str, turn_message_id: int) -> dict | None:
        for r in self.open_repairs():
            if r["chat_id"] == str(chat_id) and r["turn"] == turn_message_id:
                return r
        return None

    def waiting_for_text(self, chat_id: str) -> dict | None:
        """The repair in this chat that needs the user's next text message, if any (at most one)."""
        found = [r for r in self.all() if r["chat_id"] == str(chat_id) and r["state"] in _WAITING_FOR_TEXT]
        return found[-1] if found else None

    def update(self, repair_id: int, **fields: Any) -> None:
        """Set plain fields. The state never changes here: use transition()."""
        if "state" in fields:
            raise IllegalTransition("state changes go through transition()")
        self._change(repair_id, None, fields)

    def transition(self, repair_id: int, to: str, **fields: Any) -> dict:
        if to == APPLIED:
            raise IllegalTransition("applied is reachable only through mark_applied() with an Approval")
        return self._change(repair_id, to, fields)

    def mark_applied(self, repair_id: int, approval: Approval) -> dict:
        """The only way to reach `applied`: from awaiting-approval, on an explicit approval event.
        Call it after the edit succeeded; it does not apply anything itself."""
        if not isinstance(approval, Approval) or approval.source not in APPROVAL_SOURCES:
            raise IllegalTransition("no explicit approval event")
        return self._change(repair_id, APPLIED, {"approved_by": approval.source})

    def _change(self, repair_id: int, to: str | None, fields: dict) -> dict:
        data = self._load()
        for r in data["items"]:
            if r["id"] == repair_id:
                if to is not None:
                    if to not in TRANSITIONS.get(r["state"], ()):
                        raise IllegalTransition(f"{r['state']} -> {to}")
                    r["state"] = to
                r.update(fields)
                self._write(_REPAIRS_KEY, data)
                return r
        raise IllegalTransition(f"no repair {repair_id}")

    def choose_skill(self, repair_id: int, skill: str) -> dict:
        return self.transition(repair_id, AWAITING_ANSWER, skill=skill)

    def store_answer(self, repair_id: int, text: str) -> dict:
        """Store the user's answer as the feedback (cut at 4096) and move to drafting."""
        feedback, cut = cut_answer(text)
        return self.transition(repair_id, DRAFTING, feedback=feedback, feedback_truncated=cut)

    # -- own messages ----------------------------------------------------------------------
    def _own(self) -> dict:
        own = self._read(_OWN_KEY, {})
        if not isinstance(own, dict) or not all(isinstance(v, list) for v in own.values()):
            raise StateError("own message ids in plugin state are corrupt")
        return own

    def add_own(self, chat_id: str, message_id: Any) -> None:
        try:
            mid = int(str(message_id))
        except (TypeError, ValueError):
            return
        own = self._own()
        ids = own.setdefault(str(chat_id), [])
        if mid not in ids:
            ids.append(mid)
            del ids[:-MAX_OWN_MESSAGES_PER_CHAT]
            self._write(_OWN_KEY, own)

    def is_own(self, chat_id: str, message_id: int) -> bool:
        return message_id in self._own().get(str(chat_id), [])
