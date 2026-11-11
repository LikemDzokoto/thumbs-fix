"""Drafting: ask the user's model for one replacement, validate it, show the diff (ticket 6).

Hermes facts used: ctx.llm.complete_structured(instructions=, input=, json_schema=, system_prompt=, max_tokens=,
timeout=) is synchronous and returns a result with .parsed (agent/plugin_llm.py:500-514); ctx.dispatch_tool(
"skill_view", {"name"}) returns JSON with "success" and "content" (hermes_cli/plugins_context.py:483,
tools/skills_tool.py:613-720).
"""
import asyncio
import json
import threading
import types

import pytest

from helpers import settle
from core import drafting, hooks
from core.turns import MemoryState
from test_linker import CHAT, Gateway
from test_repairs import event, open_question, repairs

SKILL = ("---\nname: weekly-report\n---\n# Weekly report\n\n1. Collect the week's work.\n"
         "2. Format every date as MM/DD/YYYY, for example 11/09/2026.\n3. Keep it short.\n")
OLD = "Format every date as MM/DD/YYYY, for example 11/09/2026."
NEW = "Format every date as DD/MM/YYYY, for example 09/11/2026."


class FakeLlm:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls, self.threads = response, error, [], []

    def complete_structured(self, **kw):
        self.calls.append(kw)
        self.threads.append(threading.current_thread())
        if self.error:
            raise self.error
        return types.SimpleNamespace(parsed=self.response, text=json.dumps(self.response))


class Skills:
    def __init__(self, text=SKILL):
        self.text, self.calls = text, []

    def __call__(self, tool, args, **kw):
        self.calls.append((tool, args))
        return json.dumps({"success": True, "name": args["name"], "content": self.text})


def answer(llm, skills=None, text="dates should be DD/MM/YYYY"):
    """Ask the question, then deliver the user's answer on a running loop. Returns (state, adapter)."""
    state, a = open_question()
    hooks.configure(state, llm=llm, dispatch_tool=skills or Skills())

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event(text), gateway=hooks._gateway)
        await settle()
    asyncio.run(main())
    return state, a


def good():
    return FakeLlm({"old_string": OLD, "new_string": NEW})


def teardown_function(_):
    hooks.configure(MemoryState())


def test_AC_5_1_posts_a_unified_diff_of_only_the_skill_and_asks_to_approve():
    """AC-5.1: Given an open repair for weekly-report with the answer, when the plugin drafts the edit, then the
    bot posts a text diff in unified format (- removed, + added) touching only the weekly-report skill file, and
    asks the user to approve."""
    llm, skills = good(), Skills()
    state, a = answer(llm, skills)
    texts = [t for _, t in a.sent]
    assert texts[1] == drafting.DRAFTING_NOW  # right after the answer is stored, before the model is called
    diff = texts[2]
    assert diff.startswith("```\n--- a/weekly-report/SKILL.md\n+++ b/weekly-report/SKILL.md\n@@")
    assert f"\n-2. {OLD}" in diff and f"\n+2. {NEW}" in diff
    assert diff.count("--- a/") == 1 and diff.count("+++ b/") == 1
    assert "\U0001F44D" in texts[3] and "/feedback approve" in texts[3]
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval" and r["skill"] == "weekly-report"
    assert r["draft"] == {"old_string": OLD, "new_string": NEW}
    assert r["file_hash"] == drafting.file_hash(SKILL)
    assert len(r["diff_message_ids"]) == 2
    assert all(hooks._linker.repairs.is_own(CHAT, int(i)) for i in r["diff_message_ids"])
    assert skills.calls == [("skill_view", {"name": "weekly-report"})]


def test_the_model_call_is_bounded_and_off_the_event_loop():
    llm = good()
    answer(llm)
    (kw,) = llm.calls
    assert kw["timeout"] == 30 and kw["max_tokens"] == 800
    assert kw["json_schema"]["required"] == ["old_string", "new_string"]
    assert "dates should be DD/MM/YYYY" not in kw["system_prompt"] and "provider" not in kw
    assert llm.threads[0] is not threading.main_thread()  # asyncio.run's loop runs on the main thread


@pytest.mark.parametrize("error", [TimeoutError("timed out"), RuntimeError("provider 500"), PermissionError("trust")])
def test_AC_5_2_model_error_says_could_not_draft_and_stays_retryable(error):
    """AC-5.2: Given the LLM call times out or returns an error, when the plugin drafts the edit, then the bot says
    it could not draft a fix, the skill is unchanged, the repair is still open and the handler did not raise."""
    skills = Skills()
    state, a = answer(FakeLlm(error=error), skills)
    texts = [t for _, t in a.sent]
    assert texts[-1] == drafting.COULD_NOT_DRAFT and "/feedback retry" in texts[-1]
    (r,) = repairs(state)
    assert r["state"] == "drafting" and r["feedback"] and "draft" not in r
    assert skills.text == SKILL and [c[0] for c in skills.calls] == ["skill_view"]  # only ever read


def test_AC_5_2_a_hung_model_gives_up(monkeypatch):
    """AC-5.2 (timeout): a model call that never returns is abandoned after the timeout plus a grace period."""
    monkeypatch.setattr(drafting, "TIMEOUT_S", -4.95)  # wait_for(TIMEOUT_S + 5) = 0.05 s
    release = threading.Event()
    llm = FakeLlm()
    llm.complete_structured = lambda **kw: release.wait(2)
    state, a = answer(llm)
    release.set()
    assert [t for _, t in a.sent][-1] == drafting.COULD_NOT_DRAFT


def test_retry_runs_the_draft_again_and_can_succeed():
    state, a = answer(FakeLlm(error=RuntimeError("down")))
    hooks._llm = good()  # the model is back

    async def main():
        hooks._gateway = Gateway(a)
        assert hooks.feedback_command("retry") == "Drafting a fix again…"
        await settle()
    asyncio.run(main())
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval" and r["draft"]["new_string"] == NEW


def test_retry_with_nothing_to_retry_is_a_json_error():
    hooks.configure(MemoryState())
    assert hooks.feedback_command("retry") == "No repair is waiting for a retry."


@pytest.mark.parametrize("response,reason", [
    ({"old_string": "", "new_string": ""}, "empty"),
    ({"old_string": OLD, "new_string": OLD}, "identical"),
    ({"old_string": OLD, "new_string": NEW, "file_path": "other-skill/SKILL.md"}, "wrong-file"),
    ({"old_string": "not in the file", "new_string": "x"}, "not-found"),
])
def test_AC_5_3_empty_identical_or_wrong_file_drafts_are_discarded(response, reason):
    """AC-5.3: Given the LLM returns an empty edit, an edit identical to the current skill, or an edit to a
    different file, when the plugin validates the draft, then it is discarded, the bot says no change was
    proposed, and no diff is shown."""
    state, a = answer(FakeLlm(response))
    texts = [t for _, t in a.sent]
    assert texts[-1] == drafting.NO_CHANGE and len(texts) == 3  # question, drafting, no change
    assert not any("--- a/" in t for t in texts)
    (r,) = repairs(state)
    assert r["state"] == "failed" and "draft" not in r and r["failure"] == f"no change: {reason}"


def test_AC_5_4_a_long_diff_is_split_into_ordered_messages_of_at_most_4096():
    """AC-5.4: Given a diff longer than 4096 characters, when the plugin sends it, then it is split into messages
    of at most 4096 characters each in order."""
    new = "\n".join(f"- bullet {i} " + "w" * 70 for i in range(100))
    state, a = answer(FakeLlm({"old_string": OLD, "new_string": new}))
    msgs = [t for _, t in a.sent][2:-1]
    assert len(msgs) >= 2 and all(drafting._u16(m) <= 4096 for m in msgs)
    joined = "\n".join(m[4:-4] for m in msgs)
    assert joined == drafting.make_diff("weekly-report", SKILL, SKILL.replace(OLD, new))  # nothing lost, in order
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval" and len(r["diff_message_ids"]) == len(msgs) + 1


def test_AC_5_4_a_diff_too_large_is_refused_and_the_skill_is_unchanged():
    """AC-5.4: or the repair is refused as too large with the skill unchanged."""
    new = "\n".join("y" * 70 for _ in range(500))
    skills = Skills()
    state, a = answer(FakeLlm({"old_string": OLD, "new_string": new}), skills)
    assert [t for _, t in a.sent][-1] == drafting.TOO_LARGE
    (r,) = repairs(state)
    assert r["state"] == "failed" and skills.text == SKILL


def test_render_messages_edge_cases():
    assert drafting.render_messages("a\nb") == ["```\na\nb\n```"]
    emoji = "+" + "\U0001F600" * 3000  # 2 UTF-16 units each: one line of 6001 units must still split under 4096
    msgs = drafting.render_messages(emoji)
    assert msgs and all(drafting._u16(m) <= 4096 for m in msgs)
    assert drafting.render_messages("x\n```\ny") == ["x\n```\ny"]  # a diff holding a fence is sent bare
    assert drafting.render_messages("\n".join("z" * 4000 for _ in range(6))) is None


def test_a_skill_that_cannot_be_read_changes_nothing_and_is_retryable():
    def broken(tool, args, **kw):
        return json.dumps({"success": False, "error": "Skill not found"})
    state, a = answer(good(), broken)
    assert [t for _, t in a.sent][-1] == drafting.CANNOT_READ
    assert repairs(state)[0]["state"] == "drafting"


def test_no_llm_available_says_could_not_draft():
    state, a = answer(None)
    assert [t for _, t in a.sent][-1] == drafting.COULD_NOT_DRAFT


def test_a_failed_diff_send_does_not_leave_an_approvable_repair():
    state, a = open_question()
    hooks.configure(state, llm=good(), dispatch_tool=Skills())
    orig, n = a.send, {"i": 0}

    async def flaky(chat_id, content, reply_to=None, metadata=None):
        n["i"] += 1
        a.ok = n["i"] < 2  # the "Drafting a fix" ack lands, the diff does not
        return await orig(chat_id, content)
    a.send = flaky

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("dates"), gateway=hooks._gateway)
        await settle()
    asyncio.run(main())
    (r,) = repairs(state)
    assert r["state"] == "drafting" and "draft" not in r


def test_command_message_type_is_not_an_answer():
    """Ticket 5 review nit: a MessageType.COMMAND event is a command even when is_command() is absent."""
    state, a = open_question()
    e = types.SimpleNamespace(text="hello", message_type=types.SimpleNamespace(value="command"), message_id="300",
                              internal=False,
                              source=types.SimpleNamespace(platform="telegram", chat_id=CHAT, chat_type="dm"))
    assert hooks.on_inbound(event=e, gateway=Gateway(a)) is None
    assert repairs(state)[0]["state"] == "awaiting-answer"
