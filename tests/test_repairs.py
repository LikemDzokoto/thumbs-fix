"""Repair state machine, answer capture and durable queue (ticket 5).

Hermes facts used: pre_gateway_dispatch returns {"action": "skip"} to drop a message so no agent turn starts
(gateway/run_inbound_hooks.py:93-101, called at gateway/run_inbound.py:221); MessageEvent.message_type is a
MessageType (text, photo, sticker, ...; gateway/platforms/event.py:46-56) and MessageEvent.is_command() marks a
slash command (event.py:189).
"""
import json
import types

import pytest

from core import hooks
from core.guard import guard
from core.linker import BUSY, NEED_TEXT, NO_SKILL, TRUNCATED, choice_for, question_for
from core.repairs import (
    APPLIED, AWAITING_ANSWER, AWAITING_APPROVAL, AWAITING_SKILL, DRAFTING, FAILED, REJECTED, STATES, TRANSITIONS,
    Approval, IllegalTransition, RepairStore, StateError,
)
from core.turns import MemoryState
from test_linker import CHAT, FakeAdapter, react, repairs, run, setup_chat


def event(text="", kind="text", mid=300, chat=CHAT, dm=True, command=None):
    cmd = text.lstrip().startswith("/") if command is None else command
    return types.SimpleNamespace(
        text=text, message_type=types.SimpleNamespace(value=kind), message_id=str(mid), internal=False,
        source=types.SimpleNamespace(platform="telegram", chat_id=chat, chat_type="dm" if dm else "group", user_id=chat),
        is_command=lambda: cmd)


def send(adapter, *events):
    """Deliver inbound events on a running loop, like the gateway. Returns each hook result."""
    out = []
    run(adapter, *[(lambda e=e: out.append(hooks.on_inbound(event=e, gateway=hooks._gateway))) for e in events])
    return out


def open_question(turns=((100, ["weekly-report"]), (200, [])), at=150):
    state, a = setup_chat(turns=turns), FakeAdapter()
    run(a, lambda: react(at))
    return state, a


def is_skip(result):
    return isinstance(result, dict) and result["action"] == "skip"


def test_AC_4_1_next_text_is_the_feedback_and_the_hook_skips():
    """AC-4.1: Given a repair in awaiting-answer, when the next text message arrives, it is stored as the
    feedback, the state becomes drafting, and pre_gateway_dispatch returns skip so no agent turn starts."""
    state, a = open_question()
    called = []
    hooks.drafting_hook = lambda r: called.append(r["id"])
    try:
        (res,) = send(a, event("dates should be DD/MM/YYYY"))
    finally:
        hooks.drafting_hook = lambda repair: None
    assert is_skip(res)
    (r,) = repairs(state)
    assert r["feedback"] == "dates should be DD/MM/YYYY" and r["state"] == DRAFTING and called == [r["id"]]
    assert len(a.sent) == 1  # only the question; no reply to the answer


def test_a_message_with_no_repair_waiting_goes_through():
    state, a = setup_chat(), FakeAdapter()
    assert send(a, event("hello")) == [None]
    state, a = open_question()
    assert send(a, event("hi", dm=False)) == [None]  # a group message is never an answer


def test_AC_4_2_whitespace_sticker_and_photo_get_one_request_and_stay_awaiting():
    """AC-4.2: whitespace, a sticker or a photo gets one request for a short text answer, the state stays
    awaiting-answer, and the handler result is a JSON error."""
    for e in (event("   \n "), event("", kind="sticker"), event("look", kind="photo")):
        state, a = open_question()
        (res,) = send(a, e)
        assert is_skip(res)
        assert [t for _, t in a.sent][1:] == [NEED_TEXT]
        (r,) = repairs(state)
        assert r["state"] == AWAITING_ANSWER and r["feedback"] is None
        body = json.loads(hooks._linker.inbound(e).json())
        assert body["ok"] is False and body["error"] and body["state"] == AWAITING_ANSWER


def test_AC_4_3_long_emoji_and_non_latin_answers():
    """AC-4.3: 4000 characters, emoji and non-Latin script are stored without loss; longer than 4096 is cut at
    4096 with a note."""
    for text in ("x" * 4000, "Ọ̀rọ̀ yìí burú 👎🏽 — 日付は DD/MM/YYYY で、привет, مرحبا", "😀" * 4096):
        state, a = open_question()
        send(a, event(text))
        assert repairs(state)[0]["feedback"] == text and repairs(state)[0]["feedback_truncated"] is False
    state, a = open_question()
    send(a, event("é" * 4100))
    r = repairs(state)[0]
    assert r["feedback"] == "é" * 4096 and r["feedback_truncated"] is True and r["state"] == DRAFTING
    assert a.sent[-1][1] == TRUNCATED


def test_a_slash_command_is_not_an_answer():
    state, a = open_question()
    assert send(a, event("/feedback")) == [None]
    assert repairs(state)[0]["state"] == AWAITING_ANSWER and repairs(state)[0]["feedback"] is None


def test_AC_2_3_two_skills_list_both_and_take_a_name_before_the_question():
    """AC-2.3: a reply that loaded two skills lists both and proposes nothing until the user names one;
    only that one skill is on the repair."""
    state, a = open_question(turns=((100, ["a-skill", "b-skill"]),))
    assert a.sent[0][1] == choice_for(["a-skill", "b-skill"])
    assert "`a-skill`" in a.sent[0][1] and "`b-skill`" in a.sent[0][1]
    (r,) = repairs(state)
    assert r["state"] == AWAITING_SKILL and r["skill"] is None
    (res,) = send(a, event("c-skill", mid=300))
    assert is_skip(res) and repairs(state)[0]["state"] == AWAITING_SKILL and "did not recognise" in a.sent[-1][1]
    (res,) = send(a, event("  B-Skill ", mid=301))
    assert is_skip(res)
    r = repairs(state)[0]
    assert r["state"] == AWAITING_ANSWER and r["skill"] == "b-skill" and r["feedback"] is None
    assert a.sent[-1][1] == question_for("b-skill")
    (res,) = send(a, event("wrong tone", mid=302))
    r = repairs(state)[0]
    assert is_skip(res) and r["state"] == DRAFTING and r["skill"] == "b-skill" and r["feedback"] == "wrong tone"


def test_AC_12_1_second_thumbs_down_on_the_same_reply_makes_nothing():
    """AC-12.1: with a repair open for a reply, a thumbs-down on the same reply again creates no second repair
    and sends no second question, even after the answer moved it on to drafting."""
    state, a = open_question()
    run(a, lambda: react(150), lambda: react(160))
    assert len(repairs(state)) == 1 and len(a.sent) == 1
    send(a, event("wrong"))
    run(a, lambda: react(150))
    assert len(repairs(state)) == 1 and len(a.sent) == 1


def test_only_one_repair_per_chat_awaits_an_answer_and_the_other_reply_is_told():
    state, a = open_question(turns=((100, ["a"]), (200, ["b"])))
    run(a, lambda: react(250))  # a different reply, while the first awaits its answer
    assert [t for _, t in a.sent][1:] == [BUSY] and len(repairs(state)) == 1
    send(a, event("wrong", mid=300))
    run(a, lambda: react(250))  # first is drafting now: the chat is free
    assert len(repairs(state)) == 2 and repairs(state)[1]["skill"] == "b"


def test_the_answer_is_a_user_message_in_the_turn_log():
    """Follow-up: the answer's id is logged, so a later reaction cannot fall into the previous turn's range."""
    state, a = open_question()
    send(a, event("wrong", mid=300))
    turns = {t["message_id"]: t for t in hooks.get_turn_log().turns(CHAT)}
    assert turns[300]["user_message"] is True and turns[300]["skills"] == []
    run(a, lambda: react(350))  # an id after the answer must not link back to turn 100
    assert a.sent[-1][1] == NO_SKILL and len(repairs(state)) == 1
    run(a, lambda: react(300))  # the answer itself is the user's message
    assert len(a.sent) == 2


class FileState:
    """ctx.state stand-in on a JSON file; like Hermes it raises RuntimeError on an unparsable file."""

    def __init__(self, path):
        self.path = path

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise RuntimeError(f"Cannot parse plugin state {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Cannot parse plugin state {self.path}: root must be an object")
        return data

    def get(self, key, default=None):
        return self._read().get(key, default)

    def set(self, key, value):
        data = self._read()
        data[key] = value
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_approval_repair(state):
    store = RepairStore(state)
    r = store.create(CHAT, 100, ["weekly-report"])
    store.store_answer(r["id"], "UK dates")
    store.transition(r["id"], AWAITING_APPROVAL, draft={"old_string": "MM/DD", "new_string": "DD/MM"})
    return r["id"]


def test_AC_15_1_repairs_survive_a_restart_and_feedback_lists_them(tmp_path):
    """AC-15.1: an open repair awaiting approval, in plugin state, is still there after the gateway restarts
    and the user has sent a message; /feedback lists it and approval acts on the same draft."""
    path = tmp_path / "state.json"
    rid = make_approval_repair(FileState(path))
    hooks.configure(FileState(path))  # restart: a fresh process reads the file
    hooks.on_inbound(event=event("hi"), gateway=None)
    body = hooks.feedback_command("")
    assert f"#{rid} " in body and "awaiting-approval" in body
    r = hooks._linker.repairs.mark_applied(rid, Approval("feedback-approve"))
    assert r["state"] == APPLIED and r["draft"] == {"old_string": "MM/DD", "new_string": "DD/MM"}


@pytest.mark.parametrize("content", [
    "{not json", "[1, 2]", json.dumps({"repairs": "oops"}),
    json.dumps({"repairs": {"next_id": 1, "items": [{"id": "x"}]}}),
])
def test_AC_11_2_and_AC_15_2_corrupt_state_is_an_error_left_in_place_and_changes_no_skill(tmp_path, content):
    """AC-11.2 / AC-15.2: with unreadable or corrupt state every handler returns a plain-text error (the bot says it
    could not read its repairs), the data stays byte for byte, nothing is applied and no skill file changes."""
    path, skill = tmp_path / "state.json", tmp_path / "SKILL.md"
    path.write_text(content, encoding="utf-8")
    skill.write_text("dates MM/DD\n", encoding="utf-8")
    hooks.configure(FileState(path))
    for arg in ("", "approve"):
        assert "could not read" in hooks.feedback_command(arg)
    a = FakeAdapter()
    g_event, g_inbound = guard(hooks.on_platform_event), guard(hooks.on_inbound)  # as register() wraps them
    run(a, lambda: g_event(platform="telegram", event_type="reaction", payload={
        "emojis": ["👎"], "chat_id": CHAT, "message_id": "150"}))
    assert g_inbound(event=event("answer"), gateway=None) is None
    after = path.read_text(encoding="utf-8")
    try:
        before = json.loads(content)
    except ValueError:
        before = None
    if isinstance(before, dict):  # other keys (turn log) may be written; the corrupt one never is
        assert json.loads(after).get("repairs") == before.get("repairs")
    else:
        assert after == content
    assert skill.read_text(encoding="utf-8") == "dates MM/DD\n"


def test_AC_11_2_missing_state_file_is_just_empty_and_state_that_raises_is_an_error(tmp_path):
    """AC-11.2: a store that is missing reads as no repairs; one that raises is a JSON error."""
    hooks.configure(FileState(tmp_path / "none.json"))
    assert hooks.feedback_command("") == "No open repairs."

    class Broken:
        def get(self, *a):
            raise OSError("gone")

        set = get
    hooks.configure(Broken())
    assert "could not read" in hooks.feedback_command("")
    with pytest.raises(StateError):
        RepairStore(Broken()).all()


def _drive(store, rid, target):
    """Bring a fresh repair to `target` by legal moves."""
    if target == AWAITING_SKILL:
        return
    if target in (FAILED, REJECTED):
        store.transition(rid, target)
        return
    if store.get(rid)["state"] == AWAITING_SKILL:
        store.choose_skill(rid, "a")
    if target == AWAITING_ANSWER:
        return
    store.store_answer(rid, "x")
    if target == DRAFTING:
        return
    store.transition(rid, AWAITING_APPROVAL)
    if target == APPLIED:
        store.mark_applied(rid, Approval("thumbs-up"))


def test_AC_9_3_nothing_is_applied_without_an_explicit_approval_and_every_state_is_walked(tmp_path):
    """AC-9.3: walk every state and every move. Only the table's moves exist, `applied` is reachable only from
    awaiting-approval through an explicit Approval, and time or a restart changes no skill file."""
    skill = tmp_path / "SKILL.md"
    skill.write_text("dates MM/DD\n", encoding="utf-8")
    for start in STATES:
        for to in STATES:
            store = RepairStore(MemoryState())
            r = store.create(CHAT, 100, ["a", "b"] if start == AWAITING_SKILL else ["a"])
            _drive(store, r["id"], start)
            assert store.get(r["id"])["state"] == start
            if to == APPLIED:
                with pytest.raises(IllegalTransition):
                    store.transition(r["id"], to)  # never by a plain transition
                for bad in (None, "yes", Approval("agent"), Approval("")):
                    with pytest.raises(IllegalTransition):
                        store.mark_applied(r["id"], bad)
                    assert store.get(r["id"])["state"] == start
                if start == AWAITING_APPROVAL:
                    assert store.mark_applied(r["id"], Approval("thumbs-up"))["state"] == APPLIED
                else:
                    with pytest.raises(IllegalTransition):
                        store.mark_applied(r["id"], Approval("thumbs-up"))
            elif to in TRANSITIONS[start]:
                assert store.transition(r["id"], to)["state"] == to
            else:
                with pytest.raises(IllegalTransition):
                    store.transition(r["id"], to)
                assert store.get(r["id"])["state"] == start
    state = MemoryState()
    rid = make_approval_repair(state)
    assert RepairStore(state).get(rid)["state"] == AWAITING_APPROVAL  # a "restart" and no response: still waiting
    with pytest.raises(IllegalTransition):
        RepairStore(state).update(rid, state=APPLIED)
    assert skill.read_text(encoding="utf-8") == "dates MM/DD\n"
