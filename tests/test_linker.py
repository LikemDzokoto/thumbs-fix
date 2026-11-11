"""Reaction router tests. Payload shape proven against Hermes: plugins/platforms/telegram/adapter.py:2692-2718
({platform, event_type: "reaction", payload: {emojis, custom_emoji_ids, chat_id, message_id, thread_id}});
removal is emojis == []. Hook is invoked inline on the gateway loop: gateway/run_adapters.py:1679-1685.
"""
import asyncio
import threading
from dataclasses import dataclass

from core import hooks
from helpers import settle
from core.linker import NO_SKILL, REFUSAL, question_for
from core.turns import MemoryState

CHAT = "555"
UP, DOWN, HEART = "\U0001F44D", "\U0001F44E", "❤️"


@dataclass
class SendResult:
    success: bool = True
    message_id: str | None = None
    error: str | None = None
    continuation_message_ids: tuple = ()


class FakeAdapter:
    def __init__(self, first_id=900, ok=True):
        self.sent, self.next_id, self.ok = [], first_id, ok

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content))
        if not self.ok:
            return SendResult(success=False, error="boom")
        self.next_id += 1
        return SendResult(message_id=str(self.next_id))


class Gateway:
    def __init__(self, adapter):
        self.adapters = {"telegram": adapter}


def setup_chat(turns=((100, ["weekly-report"]), (200, []))):
    state = MemoryState()
    hooks.configure(state)
    state.set("turns", {CHAT: [
        {"chat_id": CHAT, "message_id": m, "skills": s, "session_id": ""} for m, s in turns]})
    return state


def react(message_id, emojis=(DOWN,), chat_id=CHAT):
    hooks.on_platform_event(
        platform="telegram", event_type="reaction",
        payload={"emojis": list(emojis), "custom_emoji_ids": [], "chat_id": chat_id,
                 "message_id": str(message_id), "thread_id": None})


def run(adapter, *reactions):
    """Fire reactions on a running loop, like the gateway, and let scheduled sends finish."""
    async def main():
        hooks._gateway = Gateway(adapter)
        for r in reactions:
            r()
        await settle()
    asyncio.run(main())


def repairs(state):
    return state.get("repairs", {"items": []})["items"]


def test_AC_3_1_thumbs_down_names_skill_and_asks():
    """AC-3.1: a thumbs-down on a logged bot reply for weekly-report posts a message with weekly-report and
    What was wrong?, and creates a repair in state awaiting-answer."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(150))
    assert len(a.sent) == 1
    chat, text = a.sent[0]
    assert chat == CHAT and "weekly-report" in text and text.endswith("What was wrong?")
    (r,) = repairs(state)
    assert r["state"] == "awaiting-answer" and r["skill"] == "weekly-report" and r["turn"] == 100
    assert r["question_message_id"] == "901"


def test_AC_3_2_every_message_of_a_split_reply_links_to_the_same_turn():
    """AC-3.2: a reaction on any message of a split reply links to that turn by id range, names its skill and
    creates exactly one repair."""
    for mid in (101, 150, 199):
        state, a = setup_chat(), FakeAdapter()
        run(a, lambda: react(mid))
        assert len(a.sent) == 1 and "weekly-report" in a.sent[0][1]
        assert len(repairs(state)) == 1
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(110), lambda: react(120))  # second thumbs-down on the same turn
    assert len(repairs(state)) == 1 and len(a.sent) == 1


def test_AC_3_3_other_emoji_and_removals_do_nothing_multi_emoji_counts():
    """AC-3.3: another emoji sends nothing and creates no repair; a removal (empty list) is nothing; a
    multi-emoji event counts when thumbs-down is among them."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(150, [UP]), lambda: react(150, []), lambda: react(150, [HEART, "\U0001F525"]))
    assert a.sent == [] and repairs(state) == []
    run(a, lambda: react(150, [HEART, DOWN]))
    assert len(a.sent) == 1 and len(repairs(state)) == 1


def test_AC_3_4_question_is_short_plain_text_with_one_question_mark():
    """AC-3.4: at most 300 characters, exactly one question mark, no markup except the skill in code format."""
    for skill in ("weekly-report", "x" * 500, "odd`name?here"):
        q = question_for(skill)
        assert len(q) <= 300 and q.count("?") == 1 and q.endswith("What was wrong?")
        assert q.count("`") == 2


def test_AC_8_1_unknown_id_is_refused_and_changes_nothing():
    """AC-8.1: a message not in the log gets exactly the refusal; no repair is created."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(50))  # before the first known turn
    assert a.sent == [(CHAT, "I can't tell which skill made this, nothing changed.")]
    assert a.sent[0][1] == REFUSAL and repairs(state) == []


def test_AC_2_2_reply_whose_turn_has_no_skill_is_refused():
    """AC-2.2: a thumbs-down on a reply with no skill behind it says so in its own words and changes nothing."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(250))  # turn 200 loaded no skill
    assert [t for _, t in a.sent] == ["No skill was behind this reply, nothing changed."]
    assert a.sent[0][1] == NO_SKILL != REFUSAL and repairs(state) == []


def test_AC_8_2_dropped_turn_is_refused_not_matched_to_a_newer_turn():
    """AC-8.2: a turn dropped by the retention limit gives the refusal; no newer turn is used."""
    state, a = setup_chat(turns=((1000, ["weekly-report"]),)), FakeAdapter()
    run(a, lambda: react(150))  # belongs to a turn that is no longer logged
    assert [t for _, t in a.sent] == [REFUSAL] and repairs(state) == []


def test_AC_8_3_own_and_user_messages_create_nothing():
    """AC-8.3: a reaction on the user's own message, or on a plugin message, is not a bad reply."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(150))  # question, id 901 recorded as own
    n = len(a.sent)
    run(a, lambda: react(901), lambda: react(100), lambda: react(200))
    assert len(a.sent) == n and len(repairs(state)) == 1
    run(a, lambda: react(50))  # refusal; its id is own too
    ref_id = state.get("own_messages")[CHAT][-1]
    before = len(a.sent)
    run(a, lambda: react(ref_id))
    assert len(a.sent) == before


def test_AC_8_4_group_chat_gets_nothing_and_private_chat_without_log_changes_nothing():
    """AC-8.4: a reaction in a group sends nothing and starts no repair; a private chat with no turn log
    starts no repair."""
    state, a = setup_chat(), FakeAdapter()
    run(a, lambda: react(150, chat_id="-100123"))
    assert a.sent == [] and repairs(state) == []
    run(a, lambda: react(150, chat_id="777"))
    assert repairs(state) == [] and all(t == REFUSAL for _, t in a.sent)


def test_failed_send_marks_repair_failed_and_a_retry_works():
    state, a = setup_chat(), FakeAdapter(ok=False)
    run(a, lambda: react(150))
    assert repairs(state)[0]["state"] == "failed"
    a.ok = True
    run(a, lambda: react(150))
    assert [r["state"] for r in repairs(state)] == ["failed", "awaiting-answer"]


def test_no_gateway_yet_does_not_raise():
    state = setup_chat()

    async def main():
        hooks._gateway = None
        react(150)
        await settle()
    asyncio.run(main())
    assert repairs(state)[0]["state"] == "failed"


def test_send_from_another_thread_uses_the_remembered_loop():
    state, a = setup_chat(), FakeAdapter()

    async def main():
        hooks._gateway, hooks._loop = Gateway(a), asyncio.get_running_loop()
        t = threading.Thread(target=react, args=(150,))
        t.start()
        await asyncio.get_running_loop().run_in_executor(None, t.join)
        await settle(lambda: a.sent)
    asyncio.run(main())
    assert len(a.sent) == 1 and len(repairs(state)) == 1
