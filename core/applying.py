"""Applying: approve or reject a repair, back up the skill, patch it through Hermes, remember to tell the agent.

Hermes-free (ADR 0004, ADR 0005). The Hermes boundary is injected by core/hooks.py:
  view_skill(name) -> dict   ctx.dispatch_tool("skill_view") parsed: {"content", "skill_dir", ...} (tools/skills_tool.py:700-702)
  dispatch(tool, args) -> str  ctx.dispatch_tool (hermes_cli/plugins_context.py:483)
  data_dir -> Path | None    ctx.state.data_dir = HERMES_HOME/plugin-data/<plugin> (hermes_cli/plugins_state.py:140)

Why these checks:
  * skill_view's content is preprocessed (${HERMES_SKILL_DIR}/${HERMES_SESSION_ID} are expanded,
    agent/skill_preprocessing.py:36-44), so the stale check compares skill_view's content hash with the one
    drafting stored (never the raw file), and the backup is the raw bytes of <skill_dir>/SKILL.md.
  * skill_manage patch matches the RAW file (tools/skill_manager_tool.py:546-552). A draft containing an expanded
    substitution value (the skill dir path or a session id) cannot match it, so it is refused.
  * with skills.write_approval on, skill_manage only stages the write (tools/skill_manager_tool.py:673-689,
    tools/write_approval.py:209): that is reported as staged, never as applied.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .drafting import file_hash
from .repairs import _OPEN as OPEN_STATES
from .repairs import APPLIED, AWAITING_APPROVAL, FAILED, REJECTED, Approval, IllegalTransition, RepairStore, StateError
from .turns import TurnLog

log = logging.getLogger("thumbs-fix")

NO_DIFF = "There is no diff to approve, so nothing was changed."
NOT_FOUND = "I could not find that repair, so nothing was changed."
STALE = "The skill changed since I drew up this diff, so nothing was changed. Thumbs-down the reply again to start over."
EXPANDED = ("This edit quotes text that Hermes fills in when it loads the skill (its folder path or a session id), "
            "so it cannot be applied to the file. Nothing was changed.")
NO_MATCH = "The text to replace is not in the skill file exactly once, so nothing was changed."
NO_BACKUP = "I could not back up the skill file first, so nothing was changed."
REJECTED_TEXT = "Rejected. Nothing changed."
REJECTED_STAGED_TEXT = ("Rejected. The edit to `{skill}` is still waiting in Hermes' /skills approval, so discard it "
                        "there too; until you do, approving it there would still apply it.")
NOTHING_TO_UNDO = "There is nothing to undo."
STAGED_ALREADY = "That edit is already waiting for your approval in Hermes (/skills), so nothing more was changed."
NOTE = ("The {skills} skill was just changed by thumbs-fix at the user's request. Your earlier copy of it is out "
        "of date: re-read it with skill_view before you use it, and follow the new text.")


def applied_text(skill: str) -> str:
    return f"Applied. The `{skill}` skill is changed. A backup is saved, and the next reply will use the new text."


def staged_text(skill: str) -> str:
    return (f"Staged, not applied. Hermes holds skill edits for approval (skills.write_approval is on), so `{skill}` "
            "is unchanged until you approve it there.")


def failed_text(skill: str, why: str) -> str:
    return f"The edit to `{skill}` could not be applied ({why}). Nothing changed."


class Applier:
    def __init__(self, repairs: RepairStore, turns: TurnLog, view_skill: Any, dispatch: Any, data_dir: Any) -> None:
        self.repairs, self.turns, self.view_skill, self.dispatch, self._data_dir = repairs, turns, view_skill, dispatch, data_dir
        self._lock = threading.Lock()  # one apply at a time: backup, patch and state move as a unit

    # -- finding the repair ----------------------------------------------------------------
    def for_message(self, chat_id: str, message_id: int) -> dict | None:
        """The open repair a message belongs to: its diff messages, or its question."""
        for r in reversed(self.repairs.open_repairs()):
            if r["chat_id"] != str(chat_id):
                continue
            if str(message_id) in [str(i) for i in r.get("diff_message_ids") or []] or str(r.get("question_message_id")) == str(message_id):
                return r
        return None

    # -- reject ----------------------------------------------------------------------------
    def reject(self, repair: dict | None) -> dict:
        """Reject an open repair (any open state: the user may drop one left half way). A rejected repair is final."""
        if not isinstance(repair, dict) or repair.get("id") is None:
            return {"ok": False, "text": NOT_FOUND, "error": "no repair"}
        with self._lock:
            try:
                current = self.repairs.get(repair["id"])  # read under the lock: an approve may have just moved it
                if current is None:
                    return {"ok": False, "text": NOT_FOUND, "error": "no repair"}
                if current["state"] not in OPEN_STATES:
                    return {"ok": False, "text": f"That repair is already {current['state']}, so nothing was changed.",
                            "error": "not open"}
                self.repairs.transition(current["id"], REJECTED)
            except (IllegalTransition, StateError) as exc:
                return {"ok": False, "text": NOT_FOUND, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - a reject must hand back a result
                log.exception("thumbs-fix: rejecting repair failed")
                return {"ok": False, "text": "Something went wrong, so nothing was changed.", "error": "internal error"}
        text = REJECTED_STAGED_TEXT.format(skill=current["skill"]) if current.get("staged") else REJECTED_TEXT
        return {"ok": True, "text": text, "state": REJECTED, "repair": current["id"]}

    # -- undo ------------------------------------------------------------------------------
    def undo(self, chat_id: str | None = None) -> dict:
        """Put back the bytes the most recent applied repair replaced. Blocking; never raises."""
        try:
            with self._lock:
                return self._undo(chat_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("thumbs-fix: undo failed")
            return {"ok": False, "text": "Something went wrong, so nothing was changed.", "error": "internal error"}

    def _undo(self, chat_id: str | None) -> dict:
        done = [r for r in self.repairs.all() if r["state"] == APPLIED and not r.get("undone")
                and (chat_id is None or r["chat_id"] == str(chat_id))]
        repair = done[-1] if done else None
        backup = Path(repair["backup_path"]) if repair and repair.get("backup_path") else None
        if repair is None or backup is None or not backup.is_file():
            return {"ok": False, "text": NOTHING_TO_UNDO, "error": "nothing to undo"}
        skill, saved = repair["skill"], backup.read_bytes()
        try:
            skill_dir = self.view_skill(skill).get("skill_dir")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": f"I could not find the `{skill}` skill, so nothing was changed.", "error": str(exc)[:200]}
        target = Path(skill_dir or "") / "SKILL.md"
        # Through Hermes first: skill_manage patch with `content` and no old_string is its full rewrite
        # (tools/skill_manager_tool.py:_act_patch), so its validation, lock, ledger and cache clearing all run.
        try:
            reply = self.dispatch("skill_manage", {"action": "patch", "name": skill, "content": saved.decode("utf-8")})
            data = json.loads(reply) if isinstance(reply, str) else reply
        except Exception as exc:  # noqa: BLE001 - includes non-UTF-8 backups
            data = {"success": False, "error": "Hermes did not answer the skill edit"}
        if not isinstance(data, dict):
            data = {"success": False, "error": "unreadable reply"}
        if data.get("success") and data.get("staged"):  # write approval is on: never go round the user's gate
            return {"ok": False, "text": f"Staged, not restored. Hermes holds skill edits for approval, so `{skill}` is unchanged until you approve it in /skills.", "staged": True}
        if not data.get("success"):
            # A real refusal (frontmatter, size budget, security scan): tell the user and leave the file alone (ADR 0004).
            why = str(data.get("error") or "skill_manage refused the restore")[:200].rstrip(". ")
            return {"ok": False, "text": f"Hermes would not restore `{skill}`: {why}. Nothing was changed.", "error": why}
        if not skill_dir or target.read_bytes() != saved:
            # skill_manage succeeded but wrote different bytes (for example it normalised line endings): "undo" must
            # give back the exact bytes, so write the backup directly, atomically, into the skill's own file.
            if not skill_dir:
                return {"ok": False, "text": f"I could not restore `{skill}`. Nothing was changed.", "error": "skill has no directory"}
            tmp = target.with_name(".SKILL.md.thumbs-undo")
            tmp.write_bytes(saved)
            os.replace(tmp, target)
            if target.read_bytes() != saved:
                return {"ok": False, "text": f"I could not verify the restored `{skill}`; please check it by hand.", "error": "verify failed"}
        self.repairs.update(repair["id"], undone=True, notice_pending=True)
        return {"ok": True, "text": f"Undone. The `{skill}` skill is back to how it was before the repair. The next reply will re-read it.",
                "state": APPLIED, "repair": repair["id"]}

    # -- approve ---------------------------------------------------------------------------
    def approve(self, repair: dict | None, source: str) -> dict:
        """Apply the repair's draft. Blocking (skill_view, file IO, skill_manage); never raises."""
        if not isinstance(repair, dict) or repair.get("id") is None:
            return {"ok": False, "text": NOT_FOUND, "error": "no repair"}
        try:
            with self._lock:
                return self._approve(repair["id"], source)
        except (StateError, IllegalTransition) as exc:
            log.warning("thumbs-fix: approve could not update state: %s", exc)
            return {"ok": False, "text": "I could not update my records, so please check the skill by hand.", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("thumbs-fix: approving repair %s failed", repair.get("id"))
            return {"ok": False, "text": "Something went wrong, so nothing was changed.", "error": "internal error"}

    def _fail(self, rid: int, skill: str, why: str, text: str) -> dict:
        self.repairs.transition(rid, FAILED, failure=why)
        return {"ok": False, "text": text, "error": why, "state": FAILED, "repair": rid}

    def _approve(self, rid: int, source: str) -> dict:
        repair = self.repairs.get(rid)  # fresh: the caller's copy may be stale
        if repair is None or repair["state"] != AWAITING_APPROVAL or not isinstance(repair.get("draft"), dict):
            return {"ok": False, "text": NO_DIFF, "error": "no diff", "repair": rid}
        if repair.get("staged"):
            return {"ok": False, "text": STAGED_ALREADY, "error": "already staged", "repair": rid}
        skill = repair["skill"]
        old, new = repair["draft"]["old_string"], repair["draft"]["new_string"]
        # 1. stale check: the same hash drafting stored, of skill_view's content
        try:
            view = self.view_skill(skill)
        except Exception as exc:  # noqa: BLE001
            return self._fail(rid, skill, f"cannot read skill: {exc}"[:200], failed_text(skill, "I could not read it"))
        if file_hash(view["content"]) != repair.get("file_hash"):
            return self._fail(rid, skill, "skill changed since the diff", STALE)
        # 2. expanded substitution values cannot match the raw file
        skill_dir = view.get("skill_dir")
        if any(v and (v in old or v in new) for v in self._expanded_values(repair, skill_dir)):
            return self._fail(rid, skill, "draft contains an expanded substitution value", EXPANDED)
        # 3. the raw file, and that the draft matches it exactly once
        if not skill_dir:
            return self._fail(rid, skill, "skill has no directory", failed_text(skill, "I cannot find its file"))
        target = Path(skill_dir) / "SKILL.md"
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return self._fail(rid, skill, f"cannot read file: {exc}"[:200], failed_text(skill, "I could not read its file"))
        if raw.decode("utf-8-sig", errors="replace").count(old) != 1:
            return self._fail(rid, skill, "old_string is not in the raw file exactly once", NO_MATCH)
        # 4. backup first, keyed by repair id (for /feedback undo)
        try:
            backup = self._backup(rid, raw)
        except OSError as exc:
            return self._fail(rid, skill, f"backup failed: {exc}"[:200], NO_BACKUP)
        # 5. apply through Hermes
        try:
            reply = self.dispatch("skill_manage", {"action": "patch", "name": skill, "old_string": old, "new_string": new})
            data = json.loads(reply) if isinstance(reply, str) else reply
        except Exception as exc:  # noqa: BLE001
            data = {"success": False, "error": "Hermes did not answer the skill edit"}
        if not isinstance(data, dict):
            data = {"success": False, "error": "unreadable reply"}
        if data.get("success") and data.get("staged"):
            self.repairs.update(rid, staged=True, pending_id=data.get("pending_id"), backup_path=str(backup))
            return {"ok": True, "text": staged_text(skill), "staged": True, "repair": rid, "state": AWAITING_APPROVAL}
        if not data.get("success"):
            why = str(data.get("error") or "skill_manage refused the edit")[:200]
            if target.read_bytes() != raw:  # should never happen; say so rather than claim nothing changed
                log.error("thumbs-fix: skill file changed although skill_manage reported an error (repair %s)", rid)
            return self._fail(rid, skill, why, failed_text(skill, why))
        self.repairs.update(rid, backup_path=str(backup), backup_sha256=file_hash(raw.decode("utf-8", "replace")),
                            notice_pending=True)
        self.repairs.mark_applied(rid, Approval(source))
        return {"ok": True, "text": applied_text(skill), "state": APPLIED, "repair": rid, "backup": str(backup)}

    def _expanded_values(self, repair: dict, skill_dir: Any) -> list[str]:
        """What ${HERMES_SKILL_DIR} and ${HERMES_SESSION_ID} turn into (agent/skill_preprocessing.py:40-44)."""
        sessions = {str(t.get("session_id") or "") for t in self.turns.turns(repair["chat_id"])}
        try:
            bound = self.turns.state.get("sessions", {})
            sessions |= {k for k, chat in bound.items() if str(chat) == repair["chat_id"]}
        except Exception:  # noqa: BLE001
            pass
        return [str(skill_dir or "")] + sorted(s for s in sessions if s)

    def _backup(self, rid: int, raw: bytes) -> Path:
        base = self._data_dir() if callable(self._data_dir) else self._data_dir
        if not base:
            raise OSError("no plugin data directory")
        folder = Path(base) / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{rid}-SKILL.md"
        tmp = folder / f".{rid}-SKILL.md.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, path)
        if path.read_bytes() != raw:
            raise OSError("backup does not match the file")
        return path

    # -- the note for the agent ------------------------------------------------------------
    def take_note(self, chat_id: str) -> str | None:
        """Context for the next pre_llm_call in this chat: which skills changed. Returned once."""
        if not chat_id:
            return None
        pending = [r for r in self.repairs.all() if r["chat_id"] == str(chat_id) and r.get("notice_pending")]
        if not pending:
            return None
        for r in pending:
            self.repairs.update(r["id"], notice_pending=False)
        names = ", ".join(dict.fromkeys(f"`{r['skill']}`" for r in pending))
        return NOTE.format(skills=names)
