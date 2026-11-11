"""Ticket 9: hostile inputs, plain messages and latency.

AC-11.1: every handler returns a JSON-able result with an error field and never raises on bad input.
AC-11.4: every user-facing failure is plain language, no stack trace or raw JSON.
AC-7.4: the plugin adds under 5 s of its own latency per step, excluding the model call.
"""
import asyncio
import json
import re
import types

import pytest

from core import hooks
from core.guard import guard
from core.turns import MemoryState

DOWN = "\U0001F44E"
BAD = [None, 0, 1.5, True, "", "x", [], [None], {}, {"a": 1}]


def _payloads():
    out = [None, "", 7, [], {}, {"emojis": None}, {"emojis": [DOWN]}, {"emojis": [DOWN], "chat_id": None, "message_id": None},
           {"emojis": [DOWN], "chat_id": "555"}, {"emojis": [DOWN], "chat_id": "555", "message_id": "abc"},
           {"emojis": DOWN, "chat_id": ["x"], "message_id": {}}, {"emojis": [None, 3], "chat_id": 555, "message_id": 1},
           {"emojis": 5}, {"emojis": [DOWN], "chat_id": "555", "message_id": float("nan")}]
    return out + [{k: v} for k in ("emojis", "chat_id", "message_id") for v in BAD]


def _json_ok(value):
    json.dumps(value)  # raises TypeError if not serialisable


def _fresh():
    state = MemoryState()
    hooks.configure(state)
    return state


def _has_error(result):
    return isinstance(result, dict) and result.get("ok") is False and bool(result.get("error"))


@pytest.mark.parametrize("payload", _payloads(), ids=lambda p: repr(p)[:50])
@pytest.mark.parametrize("platform,etype", [("telegram", "reaction"), (None, None), ("telegram", None), (5, "reaction"), ("telegram", ["x"])])
def test_AC_11_1_reaction_router_survives_hostile_payloads(platform, etype, payload):
    """AC-11.1: the reaction handler returns a JSON-able result and never raises; an unusable payload for a
    reaction is an error result and sends nothing."""
    _fresh()
    out = hooks._linker.route(platform, etype, payload)
    result = json.loads(out.json())
    assert out.text is None or isinstance(out.text, str)
    if platform == "telegram" and etype == "reaction" and not isinstance(payload, dict):
        assert _has_error(result) and out.text is None


def _events():
    S = types.SimpleNamespace
    return [None, "x", 5, {}, S(), S(source=None), S(source=S()), S(source=S(platform="telegram")),
            S(source=S(platform="telegram", chat_type="dm", chat_id=None), text=None),
            S(source=S(platform="telegram", chat_type="dm", chat_id="555"), text=5, message_type=7, message_id=object()),
            S(source=S(platform="telegram", chat_type="dm", chat_id="555", user_id=[1]), text=["a"], message_type=None, message_id=[], is_command=5),
            S(source=S(platform=3, chat_type=4, chat_id=555), text={}, message_type=S(value=None), message_id="zz", internal="yes"),
            S(source=S(platform="telegram", chat_type="dm", chat_id="555"), text="hello", message_type=S(value="text"), message_id="300")]


@pytest.mark.parametrize("event", _events(), ids=lambda e: f"{type(e).__name__}-{_events().index(e) if e in _events() else 0}")
def test_AC_11_1_inbound_and_turn_log_survive_hostile_events(event):
    """AC-11.1: inbound handling and turn logging never raise and give JSON-able results, even with a repair waiting;
    the hook returns None or a skip dict."""
    state = _fresh()
    state.set("turns", {"555": [{"chat_id": "555", "message_id": 100, "skills": ["weekly-report"], "session_id": ""}]})
    hooks._linker.repairs.create("555", 100, ["weekly-report"])
    _json_ok(hooks._turn_log.record_inbound(event))
    _json_ok(hooks._turn_log.mark_user_message(event))
    out = hooks._linker.inbound(event)
    _json_ok(json.loads(out.json()))
    r = hooks.on_inbound(event=event, gateway=None)
    assert r is None or (r["action"] == "skip" and isinstance(r["reason"], str))


@pytest.mark.parametrize("kw", [{}, {"tool_name": "skill_view"}, {"tool_name": "skill_view", "args": 5, "result": object()},
                                {"tool_name": "skill_view", "args": None, "result": None, "session_id": None},
                                {"tool_name": "skill_view", "args": {"name": ["x"]}, "result": '{"success": true, "name": 5}', "session_id": 3},
                                {"tool_name": None, "result": b"\xff"}, {"tool_name": "skill_view", "result": "[1]"},
                                {"tool_name": "skill_view", "result": '{"success":true,"name":{}}'}])
def test_AC_11_1_tool_call_logging_survives_hostile_input(kw):
    """AC-11.1: tool-call logging never raises and returns a JSON-able result; when it logged nothing the result
    is an error with a reason. The hook adapter itself returns None."""
    _fresh()
    r = hooks.log_tool_call(**kw)
    _json_ok(r)
    assert _has_error(r) if kw.get("tool_name") == "skill_view" else r["ok"] is True
    assert hooks.on_tool_call(**kw) is None


@pytest.mark.parametrize("raw", [None, 5, "", "   ", "approve 9999", "reject #x", "undo", "retry zz", "demo extra", "\x00",
                                 "approve " + "9" * 5000, "ünïcode ✓", ["list"], {"a": 1}])
@pytest.mark.parametrize("chat", [None, "", "555", 5, ["x"]])
def test_AC_11_1_feedback_command_returns_plain_text(raw, chat):
    """AC-11.1: /feedback never raises, and what it returns is plain text a person can read, not JSON."""
    _fresh()
    out = hooks.feedback_command(raw, types.SimpleNamespace(chat_id=chat))
    assert isinstance(out, str) and out.strip()
    assert not out.lstrip().startswith(("{", "["))


@pytest.mark.parametrize("repair", [None, {}, {"id": 99}, {"id": "x"}, {"id": None}, 5, "x", {"id": 1, "state": "awaiting-approval"}, {"id": 1}])
def test_AC_11_1_repair_flow_approve_reject_undo_survive_bad_repairs(repair):
    """AC-11.1: approve, reject and undo return a JSON-able result with an error field on bad input and raise nothing."""
    _fresh()
    for fn in (lambda: hooks._applier.approve(repair, "x"), lambda: hooks._applier.reject(repair)):
        r = fn()
        _json_ok(r)
        assert _has_error(r) and isinstance(r["text"], str)
    for chat in (None, "", 5, "555"):
        r = hooks._applier.undo(chat)
        _json_ok(r)
        assert _has_error(r)


@pytest.mark.parametrize("repair", [None, {}, {"id": 1}, {"id": 1, "chat_id": "555"}, {"id": 1, "chat_id": "555", "skill": None, "feedback": None},
                                    {"id": 2, "chat_id": 5, "skill": ["x"], "feedback": 3}, 5])
def test_AC_11_1_drafter_survives_bad_repairs(repair):
    """AC-11.1: the drafting step returns a JSON-able result with an error field for a bad repair and raises nothing."""
    _fresh()
    r = asyncio.run(hooks._drafter.run(repair))
    _json_ok(r)
    assert _has_error(r)


@pytest.mark.parametrize("kw", [{}, {"session_id": None, "sender_id": None}, {"session_id": 5, "sender_id": ["x"], "platform": object(), "task_id": {}},
                                {"session_id": "s", "sender_id": "555", "platform": 3}])
def test_AC_11_1_pre_llm_returns_a_context_dict_or_none(kw):
    """AC-11.1: the pre_llm_call adapter never raises and returns None or a context dict."""
    _fresh()
    r = hooks.on_pre_llm(**kw)
    assert r is None or (isinstance(r, dict) and isinstance(r["context"], str))


@pytest.mark.parametrize("payload", _payloads(), ids=lambda p: repr(p)[:40])
def test_AC_11_1_platform_event_hook_returns_none(payload):
    """AC-11.1: the gateway_platform_event adapter, called raw, returns None for any payload."""
    _fresh()
    assert hooks.on_platform_event(platform="telegram", event_type="reaction", payload=payload) is None
    assert hooks.on_platform_event(platform=None, event_type=None, payload=payload) is None


# ---- AC-11.4: plain language everywhere ------------------------------------------------------------------

def _templates():
    """Every message the plugin sends or returns to a user: module constants, formatted templates and the register() default."""
    from core import applying, commands, drafting, linker
    out = {}
    for mod, names in ((applying, ("NO_DIFF", "NOT_FOUND", "STALE", "EXPANDED", "NO_MATCH", "NO_BACKUP", "REJECTED_TEXT", "REJECTED_STAGED_TEXT",
                                   "NOTHING_TO_UNDO", "STAGED_ALREADY")),
                       (commands, ("NONE_OPEN", "NO_LIST", "UNREADABLE")),
                       (drafting, ("DRAFTING_NOW", "COULD_NOT_DRAFT", "NO_CHANGE", "TOO_LARGE", "CANNOT_READ", "APPROVE_HINT")),
                       (linker, ("REFUSAL", "NO_SKILL", "BUSY", "NEED_TEXT", "TRUNCATED"))):
        for n in names:
            out[f"{mod.__name__}.{n}"] = getattr(mod, n)
    out["REJECTED_STAGED_TEXT(fmt)"] = applying.REJECTED_STAGED_TEXT.format(skill="weekly-report")
    out["applied_text"] = applying.applied_text("weekly-report")
    out["staged_text"] = applying.staged_text("weekly-report")
    out["failed_text"] = applying.failed_text("weekly-report", "Permission denied")
    out["question_for"] = linker.question_for("weekly-report")
    out["choice_for"] = linker.choice_for(["a", "b"])
    out["register default"] = _register_default()
    return out


def _register_default():
    from helpers import load_plugin
    return load_plugin().FEEDBACK_FALLBACK


_BAD_WORDS = ("Traceback", "Exception", "Error:", 'File "', "KeyError", "TypeError", "ValueError", "RuntimeError", "None", "null", "NaN", "0x")


@pytest.mark.parametrize("name,text", list(_templates().items()))
def test_AC_11_4_every_message_template_is_plain_language(name, text):
    """AC-11.4: no template a user can see holds a stack trace, a raw JSON object or an exception name; each is a sentence."""
    assert isinstance(text, str) and text.strip()
    assert not text.lstrip().startswith(("{", "["))
    for bad in _BAD_WORDS:
        assert bad not in text, (name, bad)
    assert not re.search(r'"\w+"\s*:', text), name  # no "key": value
    assert text.rstrip()[-1] in ".?!…`)", name


def test_AC_11_4_unlinked_reaction_llm_outage_and_empty_feedback_are_plain(tmp_path):
    """AC-11.4: the three failures shown in the demo (an unlinked reaction, an LLM outage, an empty /feedback) each
    give a clear plain-language message to the user, with no stack trace or raw JSON."""
    from test_applying import FakeHermes
    from test_drafting import FakeLlm
    from test_linker import CHAT, FakeAdapter, react, run, setup_chat
    from test_repairs import event, open_question
    # unlinked: thumbs-down on a message no turn contains
    setup_chat(turns=())
    a = FakeAdapter()
    run(a, lambda: react(150))
    assert a.sent and a.sent[0][1] == "I can't tell which skill made this, nothing changed."
    # empty /feedback
    assert hooks.feedback_command("", types.SimpleNamespace(chat_id=CHAT)) == "No open repairs."
    # LLM outage
    state, a = open_question()
    fake = FakeHermes(tmp_path)

    class Down:
        def complete_structured(self, **kw):
            raise RuntimeError("provider exploded: {\"error\": 503}")
    hooks.configure(state, llm=Down(), dispatch_tool=fake, data_dir=tmp_path / "data")

    async def main():
        hooks._gateway = type("G", (), {"adapters": {"telegram": a}})()
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        from helpers import settle
        await settle()
    asyncio.run(main())
    said = [t for _, t in a.sent]
    assert drafting_text("COULD_NOT_DRAFT") in said
    for t in said:
        assert "provider exploded" not in t and "503" not in t and "Traceback" not in t and not t.lstrip().startswith("{")


def drafting_text(name):
    from core import drafting
    return getattr(drafting, name)


def test_AC_11_4_internal_failures_never_show_exception_names(tmp_path):
    """AC-11.4: when approve, reject or undo hit an unexpected error the user sees a plain sentence, not the exception."""
    _fresh()
    r = hooks._linker.repairs.create("555", 1, ["weekly-report"])

    def explode(*a, **k):
        raise KeyError("secret")
    hooks._applier.repairs.get = explode
    for res in (hooks._applier.approve(r, "x"), hooks._applier.reject(r), hooks._applier.undo("555")):
        assert "KeyError" not in res["text"] and "secret" not in res["text"]


# ---- AC-7.4: plugin overhead per step --------------------------------------------------------------------

BUDGET_S = 5.0


def test_AC_7_4_each_step_adds_under_five_seconds_excluding_the_model_call(tmp_path):
    """AC-7.4: with instant fakes for Telegram, Hermes and the model, each step of the flow (reaction to question,
    answer to draft start, draft result to diff, approve to applied) takes the plugin under 5 s of its own time."""
    import time
    from test_applying import DOWN as _D, FakeHermes, UP
    from test_drafting import good
    from test_linker import CHAT, FakeAdapter, Gateway, setup_chat
    from helpers import settle

    state = setup_chat()
    fake = FakeHermes(tmp_path)
    a = FakeAdapter()
    hooks.configure(state, llm=good(), dispatch_tool=fake, data_dir=tmp_path / "data")
    steps = {}

    async def main():
        hooks._gateway = Gateway(a)
        t = time.perf_counter()
        hooks.on_platform_event(platform="telegram", event_type="reaction",
                                payload={"emojis": [_D], "chat_id": CHAT, "message_id": "150"})
        await settle(lambda: len(a.sent) >= 1)
        steps["reaction to question"] = time.perf_counter() - t
        assert "What was wrong?" in a.sent[0][1]

        from test_repairs import event
        t = time.perf_counter()
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        await settle(lambda: len(a.sent) >= 2)
        steps["answer to draft start"] = time.perf_counter() - t

        t = time.perf_counter()
        await settle(lambda: any("-" in m or "+" in m for _, m in a.sent[2:]) and len(a.sent) >= 4)
        steps["draft result to diff"] = time.perf_counter() - t

        (r,) = state.get("repairs")["items"]
        t = time.perf_counter()
        hooks.on_platform_event(platform="telegram", event_type="reaction",
                                payload={"emojis": [UP], "chat_id": CHAT, "message_id": r["diff_message_ids"][0]})
        await settle(lambda: state.get("repairs")["items"][0]["state"] == "applied" and "Applied" in a.sent[-1][1])
        steps["approve to applied"] = time.perf_counter() - t

    asyncio.run(main())
    assert set(steps) == {"reaction to question", "answer to draft start", "draft result to diff", "approve to applied"}
    for name, secs in steps.items():
        assert secs < BUDGET_S, f"{name} took {secs:.2f}s"


# ---- nit (c): undo reports a real refusal and leaves the file alone --------------------------------------

def test_nit_c_undo_reports_a_skill_manage_refusal_and_leaves_the_file_alone(tmp_path):
    """Nit (c), ADR 0004: when skill_manage refuses the restore (frontmatter, budget, security scan), undo says so
    in plain words and does not write the file itself."""
    from test_applying import FakeHermes, RAW, UP, diff_id, drafted
    from test_linker import react, run, repairs

    state, a, fake = drafted(tmp_path)
    run(a, lambda: react(diff_id(state), (UP,)))
    applied = fake.file.read_bytes()
    assert applied != RAW.encode()

    inner = fake.__call__

    def refuse(tool, args, **kw):
        if tool == "skill_manage" and args.get("content"):
            return json.dumps({"success": False, "error": "SKILL.md frontmatter is missing"})
        return inner(tool, args, **kw)
    hooks._dispatch = refuse
    text = hooks.feedback_command("undo")
    assert "frontmatter is missing" in text and "Nothing was changed" in text and not text.lstrip().startswith("{")
    assert fake.file.read_bytes() == applied  # no direct write
    assert not repairs(state)[0].get("undone")


def test_nit_c_undo_still_restores_exact_bytes_when_skill_manage_wrote_different_ones(tmp_path):
    """Nit (c): when skill_manage reports success but wrote other bytes (it normalised line endings), undo writes the
    backup itself so the restore is exact."""
    from test_applying import FakeHermes, UP, diff_id, drafted
    from test_linker import react, run

    raw = "---\r\nname: weekly-report\r\n---\r\n# Weekly report\r\nFormat every date as MM/DD/YYYY, for example 11/09/2026.\r\n"
    state, a, fake = drafted(tmp_path, raw=raw)
    run(a, lambda: react(diff_id(state), (UP,)))
    inner = fake.__call__

    def normalising(tool, args, **kw):
        if tool == "skill_manage" and args.get("content"):
            fake.file.write_bytes(args["content"].replace("\r\n", "\n").encode())
            return json.dumps({"success": True})
        return inner(tool, args, **kw)
    hooks._dispatch = normalising
    assert hooks.feedback_command("undo").startswith("Undone")
    assert fake.file.read_bytes() == raw.encode()
