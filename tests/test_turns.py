"""Turn log tests with fakes shaped like the real Hermes payloads (no Hermes imports).

Shapes proven against the Hermes source:
- pre_gateway_dispatch(event, gateway, session_store): gateway/run_inbound_hooks.py:91-94
- MessageEvent.message_id (str), .internal, .source: gateway/platforms/event.py:76-78,115
- SessionSource.chat_id / chat_type ("dm" | "group" | "channel" | "thread"): gateway/session.py:69-75;
  Telegram maps private -> "dm", group/supergroup -> "group": plugins/platforms/telegram/adapter.py:6517-6518
- pre_llm_call(session_id, task_id, platform, sender_id, ...): agent/turn_context.py:919-931
- post_tool_call(tool_name, args, result, task_id, session_id, ...) with "" for absent ids:
  model_tools.py:715-716, 674-676
- skill_view success {"success": true, "name": ...}: tools/skills_tool.py:698-699;
  failure {"success": false, "error": ...}: tools/skills_tool_plugin.py:29-30
"""
import importlib
import json
import logging
from dataclasses import dataclass, field
from enum import Enum

from helpers import load_plugin
from core.turns import MAX_TURNS_PER_CHAT, MemoryState, TurnLog


class Platform(Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"


@dataclass
class Source:
    platform: Platform = Platform.TELEGRAM
    chat_id: str = "555"
    chat_type: str = "dm"
    user_id: str = "555"


@dataclass
class Event:
    message_id: str = "100"
    source: Source = field(default_factory=Source)
    internal: bool = False
    text: str = "weekly report please"


class FakeCtx:
    def __init__(self):
        self.hooks, self.commands, self.state = {}, {}, MemoryState()

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler


SKILL_OK = json.dumps({"success": True, "name": "weekly-report", "description": "d", "content": "..."})
SKILL_FAIL = json.dumps({"success": False, "error": "Skill 'nope' not found."})
SKILL_ERR = json.dumps({"error": "boom"})


def wired():
    ctx = FakeCtx()
    load_plugin().register(ctx)
    return ctx, TurnLog(ctx.state)


def tool(ctx, result=None, name="weekly-report", **ids):
    result = json.dumps({"success": True, "name": name}) if result is None else result
    ctx.hooks["post_tool_call"](
        tool_name="skill_view", args={"name": name}, result=result,
        **{"task_id": "", "session_id": "", **ids},
    )


def test_private_telegram_inbound_is_recorded_with_its_message_id():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="100"), gateway=object(), session_store=None)
    assert log_.turns("555") == [{"chat_id": "555", "message_id": 100, "skills": [], "session_id": ""}]


def test_group_internal_and_other_platform_inbound_are_not_recorded():
    ctx, log_ = wired()
    h = ctx.hooks["pre_gateway_dispatch"]
    h(event=Event(source=Source(chat_id="-100", chat_type="group")), gateway=None)
    h(event=Event(internal=True, message_id="bg_1"), gateway=None)
    h(event=Event(message_id="not-a-number"), gateway=None)
    h(event=Event(source=Source(platform=Platform.DISCORD)), gateway=None)
    assert log_.turns("555") == [] and log_.turns("-100") == []


def test_gateway_handle_is_retained_for_later_sends():
    ctx, _ = wired()
    gw = object()
    ctx.hooks["pre_gateway_dispatch"](event=Event(), gateway=gw, session_store=None)
    hooks = importlib.import_module("thumbs_fix_plugin.core.hooks")
    assert hooks.get_gateway() is gw


def test_inbound_hook_returns_none_so_the_gateway_continues():
    ctx, _ = wired()
    assert ctx.hooks["pre_gateway_dispatch"](event=Event(), gateway=object()) is None


def test_pre_llm_binds_session_to_the_chat_and_skill_view_lands_on_the_turn():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="100"), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", task_id="T1", platform="telegram", sender_id="555", model="m")
    tool(ctx, session_id="S1", task_id="T1")
    assert log_.turns("555")[0]["skills"] == ["weekly-report"]
    assert log_.turns("555")[0]["session_id"] == "S1"


def test_skill_matches_by_task_id_alone_or_session_id_alone():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="100"), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", task_id="T1", platform="telegram", sender_id="555")
    tool(ctx, name="a", task_id="T1", session_id="")
    tool(ctx, name="b", task_id="", session_id="S1")
    assert log_.turns("555")[0]["skills"] == ["a", "b"]


def test_failed_skill_view_adds_nothing():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", platform="telegram", sender_id="555")
    tool(ctx, SKILL_FAIL, session_id="S1")
    tool(ctx, SKILL_ERR, session_id="S1")
    tool(ctx, "not json", session_id="S1")
    assert log_.turns("555")[0]["skills"] == []


def test_unbound_session_and_non_skill_tools_add_nothing():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(), gateway=None)
    tool(ctx, session_id="unknown")
    ctx.hooks["post_tool_call"](tool_name="terminal", args={}, result=SKILL_OK, task_id="", session_id="S1")
    assert log_.turns("555")[0]["skills"] == []


def test_group_sender_with_no_private_turns_does_not_bind():
    ctx, log_ = wired()
    ctx.hooks["pre_llm_call"](session_id="G1", platform="telegram", sender_id="777")
    tool(ctx, session_id="G1")
    assert log_.turns("777") == []


def test_group_message_after_dm_does_not_bind_group_session_to_private_turn():
    """Regression B1, order DM then group: the same user writes in a group after a DM."""
    ctx, log_ = wired()
    h = ctx.hooks["pre_gateway_dispatch"]
    h(event=Event(message_id="100"), gateway=None)
    h(event=Event(message_id="7", source=Source(chat_id="-100", chat_type="group", user_id="555")), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="G", task_id="TG", platform="telegram", sender_id="555")
    tool(ctx, session_id="G", task_id="TG")
    assert log_.turns("555")[0]["skills"] == [] and log_.turns("555")[0]["session_id"] == ""
    assert log_.turns("-100") == []


def test_group_then_dm_binds_the_dm_and_drops_a_stale_group_binding():
    """Regression B1, order group then DM; a session first bound as DM is unbound once a group turn follows."""
    ctx, log_ = wired()
    h = ctx.hooks["pre_gateway_dispatch"]
    h(event=Event(message_id="7", source=Source(chat_id="-100", chat_type="group", user_id="555")), gateway=None)
    h(event=Event(message_id="100"), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", platform="telegram", sender_id="555")
    tool(ctx, session_id="S1")
    assert log_.turns("555")[0]["skills"] == ["weekly-report"]
    h(event=Event(message_id="8", source=Source(chat_id="-100", chat_type="group", user_id="555")), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", platform="telegram", sender_id="555")  # stale: now a group turn
    tool(ctx, name="other", session_id="S1")
    assert log_.turns("555")[0]["skills"] == ["weekly-report"]


def test_platform_is_compared_normalised():
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", platform=Platform.TELEGRAM, sender_id="555")
    tool(ctx, session_id="S1")
    assert log_.turns("555")[0]["skills"] == ["weekly-report"]


def test_skill_goes_on_the_latest_turn_and_is_not_duplicated():
    ctx, log_ = wired()
    for mid in ("100", "110"):
        ctx.hooks["pre_gateway_dispatch"](event=Event(message_id=mid), gateway=None)
        ctx.hooks["pre_llm_call"](session_id="S1", platform="telegram", sender_id="555")
    tool(ctx, session_id="S1")
    tool(ctx, session_id="S1")
    assert [t["skills"] for t in log_.turns("555")] == [[], ["weekly-report"]]


def test_AC_2_1_reply_id_falls_in_the_range_of_the_turn_holding_the_skill():
    """AC-2.1 (unit half; the real-bot half runs at the milestone): Given the weekly-report skill loaded
    for a reply, when the user asks and the bot replies, then the turn log holds an entry for that chat
    whose message id range contains the reply's id and whose skills contain weekly-report."""
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="100"), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S1", task_id="T1", platform="telegram", sender_id="555")
    tool(ctx, session_id="S1", task_id="T1")
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="120"), gateway=None)  # next inbound closes the range
    turn = log_.turn_containing("555", 103)  # the bot reply's Telegram id
    assert turn["message_id"] == 100 and "weekly-report" in turn["skills"]
    assert log_.turn_containing("555", 121)["message_id"] == 120
    assert log_.turn_containing("555", 100) is None  # the inbound itself is not inside its own range
    assert log_.turn_containing("999", 103) is None


def test_AC_2_2_reply_with_no_skill_is_logged_with_an_empty_skill_list():
    """AC-2.2: Given a reply produced with no skill loaded, when the bot replies, then the turn log
    records it with an empty skill list (the later thumbs-down refusal reads this list)."""
    ctx, log_ = wired()
    ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="200"), gateway=None)
    ctx.hooks["pre_llm_call"](session_id="S2", platform="telegram", sender_id="555")
    turn = log_.turn_containing("555", 202)
    assert turn is not None and turn["skills"] == []


def test_turns_persist_in_state_and_are_bounded_to_200_per_chat():
    state = MemoryState()
    log_ = TurnLog(state)
    for i in range(1, MAX_TURNS_PER_CHAT + 51):
        log_.record_inbound(Event(message_id=str(i)))
    kept = log_.turns("555")
    assert len(kept) == MAX_TURNS_PER_CHAT
    assert kept[0]["message_id"] == 51 and kept[-1]["message_id"] == 250
    assert TurnLog(state).turns("555") == kept  # a new instance on the same state sees them


def test_debug_line_per_recorded_event(caplog):
    ctx, _ = wired()
    with caplog.at_level(logging.DEBUG, logger="thumbs-fix"):
        ctx.hooks["pre_gateway_dispatch"](event=Event(message_id="100"), gateway=None)
        ctx.hooks["pre_llm_call"](session_id="S1", platform="telegram", sender_id="555")
        tool(ctx, session_id="S1")
    assert "recorded chat=555 msg=100" in caplog.text
    assert "bound session=S1" in caplog.text
    assert "skill weekly-report added" in caplog.text
