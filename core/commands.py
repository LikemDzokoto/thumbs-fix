"""/feedback: list, approve, reject, undo, retry, demo. Hermes-free (ADR 0005); returns plain text and never raises.

Hermes passes the handler `source` (the SessionSource of the chat the command came from) when the handler names it
as a parameter (hermes_cli/plugin_command_dispatch.py, gateway/run_inbound_hooks.py:295-300). hooks.feedback_command
turns source.chat_id into `chat_id`, so every subcommand acts only on that chat's repairs. With no chat (the CLI),
it acts across chats: thumbs-fix is a single-owner bot.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .applying import Applier
from .repairs import AWAITING_APPROVAL, DRAFTING, RepairStore, StateError

log = logging.getLogger("thumbs-fix")

SUBCOMMANDS = ("list", "approve", "reject", "undo", "retry", "demo")
NONE_OPEN = "No open repairs. Nothing to approve or reject."
NO_LIST = "No open repairs."
UNREADABLE = "I could not read my repairs, so nothing was changed."
DEMO_NAME = "weekly-report"
DEMO_CONTENT = """---
name: weekly-report
description: Use for a weekly report or weekly status update.
---
# Weekly report

This skill is a demo made by thumbs-fix. Its report data is static SAMPLE DATA, not real work.

## Steps

1. Reply with a short weekly report in the layout below, using the sample data as it is.
2. Write every date as MM/DD/YYYY.
3. Do not add anything that is not in the sample data.

## Layout

Weekly report
Period: <start> to <end>
Done: <each finished item and its date>
Next: <each planned item and its date>

## Sample data (static, for the demo)

- Period: 2026-11-09 to 2026-11-15
- Done: Draft the outline, finished 2026-11-10
- Done: Review the draft, finished 2026-11-13
- Next: Send the summary, planned 2026-11-16
"""


class Commands:
    def __init__(self, repairs: RepairStore, applier: Applier, retry: Callable[[dict], None], dispatch: Any, view_skill: Any) -> None:
        self.repairs, self.applier, self._retry_start, self._dispatch, self._view_skill = repairs, applier, retry, dispatch, view_skill

    def handle(self, raw_args: str = "", chat_id: str | None = None) -> str:
        try:
            return self._handle(raw_args or "", chat_id)
        except StateError as exc:
            log.warning("thumbs-fix: /feedback could not use its state: %s", exc)
            return UNREADABLE
        except Exception:  # noqa: BLE001 - a command handler must hand back text
            log.exception("thumbs-fix: /feedback failed")
            return "Something went wrong, so nothing was changed."

    def _handle(self, raw: str, chat_id: str | None) -> str:
        parts = raw.split()
        sub, rest = (parts[0].lower(), parts[1:]) if parts else ("list", [])
        if sub == "list":
            return self._list(chat_id)
        if sub in ("approve", "reject"):
            return self._decide(sub, rest, chat_id)
        if sub == "undo":
            return self.applier.undo(chat_id)["text"]
        if sub == "retry":
            return self._retry(rest, chat_id)
        if sub == "demo":
            return self._demo()
        return f"I do not know `{parts[0]}`. Valid options: " + ", ".join(f"/feedback {s}" for s in SUBCOMMANDS) + ". Nothing was changed."

    # -- helpers ---------------------------------------------------------------------------
    def _open(self, chat_id: str | None) -> list[dict]:
        return [r for r in self.repairs.open_repairs() if chat_id is None or r["chat_id"] == str(chat_id)]

    @staticmethod
    def _line(r: dict) -> str:
        return f"#{r['id']} {r['skill'] or 'which skill?'}: {r['state']}{' (staged in Hermes)' if r.get('staged') else ''}"

    def _list(self, chat_id: str | None) -> str:
        open_ = self._open(chat_id)
        return "\n".join(self._line(r) for r in open_) if open_ else NO_LIST

    def _decide(self, verb: str, args: list[str], chat_id: str | None) -> str:
        open_ = self._open(chat_id)
        if not open_:
            return NONE_OPEN
        if args:
            want = args[0].lstrip("#").lower()
            hits = [r for r in open_ if str(r["id"]) == want or (r["skill"] or "").lower() == want]
            if not hits:
                return f"No open repair for `{args[0]}`. Nothing was changed.\n" + "\n".join(self._line(r) for r in open_)
            chosen = ([r for r in hits if r["state"] == AWAITING_APPROVAL] or hits)[-1]
        elif len(open_) == 1:
            chosen = open_[0]
        else:
            names = [r["skill"] or f"#{r['id']}" for r in open_]
            ref = lambda r: r["skill"] if names.count(r["skill"]) == 1 else f"#{r['id']}"  # noqa: E731
            return (f"{len(open_)} repairs are open, so I did nothing. Which one to {verb}?\n"
                    + "\n".join(self._line(r) for r in open_)
                    + f"\nReply with e.g. /feedback {verb} {ref(open_[-1])}")
        if verb == "approve":
            return self.applier.approve(chosen, "feedback-approve")["text"]
        return self.applier.reject(chosen)["text"]

    def _retry(self, args: list[str], chat_id: str | None) -> str:
        """/feedback retry [id]: draft again for a repair whose model call or diff send failed (state drafting)."""
        stuck = [r for r in self._open(chat_id) if r["state"] == DRAFTING and (not args or str(r["id"]) == args[0].lstrip("#"))]
        if not stuck:
            return "No repair is waiting for a retry."
        self._retry_start(stuck[-1])
        return "Drafting a fix again…"

    def _demo(self) -> str:
        """Create the weekly-report demo skill through skill_manage; never overwrite an existing one."""
        try:
            self._view_skill(DEMO_NAME)
            return f"A skill named `{DEMO_NAME}` already exists, so I left it alone. Nothing was changed."
        except Exception:  # noqa: BLE001 - not found (or unreadable): go on and let skill_manage create refuse if it exists
            pass
        try:
            reply = self._dispatch("skill_manage", {"action": "create", "name": DEMO_NAME, "content": DEMO_CONTENT})
            data = json.loads(reply) if isinstance(reply, str) else reply
        except Exception as exc:  # noqa: BLE001
            return "I could not create the demo skill. Nothing was changed."
        if not isinstance(data, dict) or not data.get("success"):
            why = str(data.get("error") if isinstance(data, dict) else "unreadable reply")[:200].rstrip(". ")
            return f"I could not create the demo skill: {why}. Nothing was changed."
        if data.get("staged"):
            return (f"Staged, not created. Hermes holds skill writes for approval (skills.write_approval is on), so "
                    f"`{DEMO_NAME}` does not exist until you approve it in /skills.")
        return (f"Created the sample skill `{DEMO_NAME}`. Ask me for a weekly report, then thumbs-down the reply "
                "and say what was wrong with the dates.")
