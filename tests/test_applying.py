"""Approve / reject / apply tests, with a fake Hermes skill store that behaves like skill_view and skill_manage
(content expanded for ${HERMES_SKILL_DIR}; patch matches the raw file: tools/skill_manager_tool.py:546-552).
The same flow against the real Hermes tools is in test_hermes_applying.py.
"""
import asyncio
import json
import types
from pathlib import Path

import pytest

from helpers import settle
from core import applying, drafting, hooks
from core.applying import NO_DIFF
from core.turns import MemoryState
from test_drafting import FakeLlm, good
from test_linker import CHAT, DOWN, UP, FakeAdapter, Gateway, SendResult, react, run
from test_repairs import event, open_question, repairs

RAW = ("---\nname: weekly-report\n---\n# Weekly report\n\n1. Collect the week's work.\n"
       "2. Format every date as MM/DD/YYYY, for example 11/09/2026.\n3. Keep it short.\n")
OLD = "Format every date as MM/DD/YYYY, for example 11/09/2026."
NEW = "Format every date as DD/MM/YYYY, for example 09/11/2026."


class JsonState:
    """ctx.state stand-in that lives in a file, so a new instance after 'restart' sees the old data."""

    def __init__(self, path: Path):
        self.path = path
        self.data_dir = path.parent / "plugin-data"

    def get(self, key, default=None):
        return json.loads(self.path.read_text("utf-8")).get(key, default) if self.path.exists() else default

    def set(self, key, value):
        d = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        d[key] = value
        self.path.write_text(json.dumps(d), "utf-8")


class FakeHermes:
    """skill_view + skill_manage over a real temp skill dir."""

    def __init__(self, root: Path, raw=RAW, mode="ok"):
        self.dir = root / "skills" / "weekly-report"
        self.dir.mkdir(parents=True)
        self.file = self.dir / "SKILL.md"
        self.file.write_bytes(raw.encode("utf-8"))
        self.mode, self.calls = mode, []

    def __call__(self, tool, args, **kw):
        self.calls.append((tool, args))
        if tool == "skill_view" and not self.file.exists():
            return json.dumps({"success": False, "error": "not found"})
        if tool == "skill_view":
            text = self.file.read_text("utf-8").replace("${HERMES_SKILL_DIR}", str(self.dir))
            return json.dumps({"success": True, "name": args["name"], "content": text, "skill_dir": str(self.dir)})
        assert tool == "skill_manage"
        if args["action"] == "create":
            if self.file.exists():
                return json.dumps({"success": False, "error": "already exists"})
            self.file.write_bytes(args["content"].encode("utf-8"))
            return json.dumps({"success": True, "message": "created"})
        assert args["action"] == "patch"
        if args.get("content"):  # the full rewrite: patch with `content` and no old_string
            if self.mode == "staged":
                return json.dumps({"success": True, "staged": True, "pending_id": "p2"})
            self.file.write_bytes(args["content"].encode("utf-8"))
            return json.dumps({"success": True, "message": "rewritten"})
        if self.mode == "staged":
            return json.dumps({"success": True, "staged": True, "pending_id": "p1", "message": "staged"})
        if self.mode == "error":
            return json.dumps({"success": False, "error": "Permission denied"})
        text = self.file.read_text("utf-8")
        if text.count(args["old_string"]) != 1:
            return json.dumps({"success": False, "error": "no match"})
        self.file.write_bytes(text.replace(args["old_string"], args["new_string"], 1).encode("utf-8"))
        return json.dumps({"success": True, "message": "Patched"})


def drafted(tmp_path, raw=RAW, mode="ok", llm=None, state=None):
    """A repair awaiting approval for weekly-report; returns (state, adapter, fake hermes)."""
    fake = FakeHermes(tmp_path, raw, mode)
    state, a = open_question()
    hooks.configure(state, llm=llm or good(), dispatch_tool=fake, data_dir=tmp_path / "data")

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        await settle()
    asyncio.run(main())
    return state, a, fake


def diff_id(state):
    (r,) = repairs(state)
    return r["diff_message_ids"][0]


def teardown_function(_):
    hooks.configure(MemoryState())


def test_AC_6_1_AC_7_1_thumbs_up_on_the_diff_applies_with_a_backup_and_exactly_the_diffed_lines(tmp_path):
    """AC-6.1, AC-7.1: a thumbs-up on a diff message applies the edit through skill_manage patch; the file differs
    from the backup by exactly the diffed lines, the backup holds the previous raw bytes, the bot names the skill."""
    state, a, fake = drafted(tmp_path)
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "applied" and r["approved_by"] == "thumbs-up"
    assert ("skill_manage", {"action": "patch", "name": "weekly-report", "old_string": OLD, "new_string": NEW}) in fake.calls
    backup = Path(r["backup_path"])
    assert backup == tmp_path / "data" / "backups" / f"{r['id']}-SKILL.md" and backup.read_bytes() == before
    after = fake.file.read_bytes()
    assert after == before.replace(OLD.encode(), NEW.encode())
    old_lines, new_lines = before.decode().splitlines(), after.decode().splitlines()
    changed = [(o, n) for o, n in zip(old_lines, new_lines) if o != n]
    assert len(old_lines) == len(new_lines) and changed == [("2. " + OLD, "2. " + NEW)]
    assert "`weekly-report`" in a.sent[-1][1] and "Applied" in a.sent[-1][1]


def test_AC_6_1_every_part_of_a_split_diff_approves():
    """Nit (a): ids in raw_response["message_ids"] and continuation_message_ids are all recorded, so a reaction on
    any part approves."""
    res = SendResult(message_id="7")
    res.raw_response = {"message_ids": ["7", "8", "9"]}
    res.continuation_message_ids = ("8",)
    assert hooks._sent_ids(res) == ["7", "8", "9"]


def test_AC_6_1_feedback_approve_applies_the_same_way(tmp_path):
    """AC-6.1: /feedback approve is the other approval event."""
    state, a, fake = drafted(tmp_path)
    assert "Applied" in hooks.feedback_command("approve")
    (r,) = repairs(state)
    assert r["state"] == "applied" and r["approved_by"] == "feedback-approve" and NEW.encode() in fake.file.read_bytes()


def test_AC_9_1_thumbs_down_on_the_diff_rejects_and_leaves_the_file_byte_identical(tmp_path):
    """AC-9.1: a thumbs-down on the diff closes the repair as rejected, the file is byte-identical, the bot says so."""
    state, a, fake = drafted(tmp_path)
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (DOWN,)))
    (r,) = repairs(state)
    assert r["state"] == "rejected" and fake.file.read_bytes() == before
    assert not [c for c in fake.calls if c[0] == "skill_manage"] and not (tmp_path / "data").exists()
    assert a.sent[-1][1] == applying.REJECTED_TEXT


def test_AC_9_1_feedback_reject_does_the_same(tmp_path):
    """AC-9.1: /feedback reject."""
    state, a, fake = drafted(tmp_path)
    before = fake.file.read_bytes()
    assert hooks.feedback_command("reject") == applying.REJECTED_TEXT
    assert repairs(state)[0]["state"] == "rejected" and fake.file.read_bytes() == before


def test_AC_6_3_approval_before_a_diff_exists_applies_nothing():
    """AC-6.3: still awaiting-answer; a thumbs-up on the question or /feedback approve applies nothing and says
    there is no diff to approve."""
    state, a = open_question()
    fake = FakeHermes(Path(__import__("tempfile").mkdtemp()))
    hooks.configure(state, dispatch_tool=fake)
    before = fake.file.read_bytes()
    (r,) = repairs(state)
    run(a, lambda: react(r["question_message_id"], (UP,)))
    assert a.sent[-1][1] == NO_DIFF
    assert hooks.feedback_command("approve") == NO_DIFF
    assert repairs(state)[0]["state"] == "awaiting-answer" and fake.file.read_bytes() == before
    assert fake.calls == []


def test_AC_6_4_a_skill_that_changed_since_the_diff_is_refused(tmp_path):
    """AC-6.4: the same sha256-of-skill_view-content hash drafting stored no longer matches: refuse, file as it is."""
    state, a, fake = drafted(tmp_path)
    fake.file.write_bytes(RAW.replace("Keep it short.", "Keep it very short.").encode())
    edited = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "failed" and fake.file.read_bytes() == edited
    assert a.sent[-1][1] == applying.STALE and not [c for c in fake.calls if c[0] == "skill_manage"]
    assert not (tmp_path / "data").exists()


def test_AC_6_4_the_hash_is_of_skill_view_content_not_the_raw_file(tmp_path):
    """AC-6.4: a skill using ${HERMES_SKILL_DIR} has raw bytes that differ from skill_view's content; approving
    must still work, which it only can if the hash is taken of the same preprocessed content."""
    raw = RAW + "Templates live in ${HERMES_SKILL_DIR}/templates.\n"
    state, a, fake = drafted(tmp_path, raw=raw)
    assert drafting.file_hash(raw) != repairs(state)[0]["file_hash"]
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "applied"
    assert "${HERMES_SKILL_DIR}/templates" in fake.file.read_text("utf-8")  # the substitution token is untouched
    assert Path(repairs(state)[0]["backup_path"]).read_bytes() == raw.encode()


@pytest.mark.parametrize("which", ["old", "new"])
def test_AC_6_4_a_draft_with_an_expanded_substitution_value_is_refused(tmp_path, which):
    """AC-6.4: old_string or new_string holding the expanded skill dir cannot match the raw file; refuse."""
    raw = RAW + "Templates live in ${HERMES_SKILL_DIR}/templates.\n"
    fake_dir = str(tmp_path / "skills" / "weekly-report")
    bad = ({"old_string": f"Templates live in {fake_dir}/templates.", "new_string": "Templates live in ./templates."}
           if which == "old" else {"old_string": OLD, "new_string": f"{NEW} See {fake_dir}/x"})
    state, a, fake = drafted(tmp_path, raw=raw, llm=FakeLlm(bad))
    assert repairs(state)[0]["state"] == "awaiting-approval"
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and fake.file.read_bytes() == before
    assert a.sent[-1][1] == applying.EXPANDED and not [c for c in fake.calls if c[0] == "skill_manage"]


def test_AC_6_4_a_session_id_in_the_draft_is_refused(tmp_path):
    """AC-6.4: ${HERMES_SESSION_ID} expands to the session id; a draft quoting it is refused."""
    state, a, fake = drafted(tmp_path, llm=FakeLlm({"old_string": OLD, "new_string": NEW + " sess-42"}))
    hooks.get_turn_log().state.set("turns", {CHAT: [{"chat_id": CHAT, "message_id": 100, "skills": ["weekly-report"], "session_id": "sess-42"}]})
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and fake.file.read_bytes() == before


def test_AC_7_3_an_error_result_marks_the_repair_failed_and_says_nothing_changed(tmp_path):
    """AC-7.3: skill_manage returns an error: the bot says it could not be applied, the file is unchanged, the
    repair is failed, and /feedback approve returns a JSON error."""
    state, a, fake = drafted(tmp_path, mode="error")
    before = fake.file.read_bytes()
    out = hooks.feedback_command("approve")
    assert "could not be applied" in out and "Nothing changed" in out
    (r,) = repairs(state)
    assert r["state"] == "failed" and fake.file.read_bytes() == before
    assert hooks._applier.take_note(CHAT) is None


def test_AC_7_3_a_read_only_skill_dir_fails_cleanly(tmp_path):
    """AC-7.3: a backup that cannot be written stops before skill_manage is called."""
    state, a, fake = drafted(tmp_path)
    (tmp_path / "data").write_text("a file where the folder should be")
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and fake.file.read_bytes() == before
    assert a.sent[-1][1] == applying.NO_BACKUP and not [c for c in fake.calls if c[0] == "skill_manage"]


def test_AC_7_1_a_staged_result_is_reported_as_staged_not_applied(tmp_path):
    """AC-7.1: with skills.write_approval on, skill_manage stages; the bot says staged, the repair is flagged
    staged and is not applied, and the file is unchanged."""
    state, a, fake = drafted(tmp_path, mode="staged")
    before = fake.file.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval" and r["staged"] is True and r["pending_id"] == "p1"
    assert a.sent[-1][1].startswith("Staged, not applied") and "Applied" not in a.sent[-1][1]
    assert fake.file.read_bytes() == before and hooks._applier.take_note(CHAT) is None
    assert "already waiting" in hooks.feedback_command("approve")  # no second staged write


def test_AC_7_2_the_next_pre_llm_call_says_the_skill_changed_exactly_once(tmp_path):
    """AC-7.2 (plugin side): after applying, the next pre_llm_call in that chat returns context naming the skill
    and telling the agent to re-read it; the one after returns nothing. (The reply itself is proven on the real bot.)"""
    state, a, fake = drafted(tmp_path)
    kw = dict(session_id="s1", platform="telegram", sender_id=CHAT, task_id="")
    assert hooks.on_pre_llm(**kw) is None
    run(a, lambda: react(diff_id(state), (UP,)))
    other = hooks.on_pre_llm(session_id="s9", platform="telegram", sender_id="999", task_id="")
    assert other is None
    first = hooks.on_pre_llm(**kw)
    assert set(first) == {"context"} and "weekly-report" in first["context"] and "skill_view" in first["context"]
    assert hooks.on_pre_llm(**kw) is None


def test_AC_15_1_an_awaiting_repair_survives_a_restart_and_can_still_be_approved(tmp_path):
    """AC-15.1: draft with a file-backed state, rebuild everything as a restart would (new configure, no gateway
    handle), then approve by /feedback approve and by reaction after one inbound message; the skill is applied."""
    fake = FakeHermes(tmp_path)
    state = JsonState(tmp_path / "state.json")
    hooks.configure(state)
    state.set("turns", {CHAT: [{"chat_id": CHAT, "message_id": 100, "skills": ["weekly-report"], "session_id": ""},
                               {"chat_id": CHAT, "message_id": 200, "skills": [], "session_id": ""}]})
    a = FakeAdapter()
    hooks.configure(state, llm=good(), dispatch_tool=fake, data_dir=state.data_dir)
    run(a, lambda: react(150))

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        await settle()
    asyncio.run(main())
    assert repairs(state)[0]["state"] == "awaiting-approval"
    ids = repairs(state)[0]["diff_message_ids"]
    # restart: fresh process state, fresh plugin objects over the same files
    hooks.configure(JsonState(tmp_path / "state.json"), llm=None, dispatch_tool=fake)
    assert hooks._gateway is None
    b = FakeAdapter(first_id=950)
    run(b, lambda: react(ids[1], (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "applied" and NEW.encode() in fake.file.read_bytes()
    assert Path(r["backup_path"]).read_bytes() == RAW.encode()
    assert "Applied" in b.sent[-1][1]  # the gateway handle came back with the first event
