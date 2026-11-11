"""Integration: the REAL Hermes gateway_platform_event machinery calls the plugin, which answers through a
fake adapter. Skipped where Hermes is not installed; scripts/check-hermes.sh runs it in the WSL venv.

What is real: PluginManager/PluginContext registration (hermes_cli/plugins*.py), the Telegram adapter's
_normalize_reaction_event (the documented payload), GatewayRunner._handle_gateway_platform_event
(gateway/run_adapters.py:1679) and the lifecycle invoke_hook path. What is fake: the adapter that receives send().
"""
import asyncio
import sys
import types

import pytest

pytest.importorskip("hermes_cli.plugins", reason="Hermes is not installed on this host")

from helpers import load_plugin, settle  # noqa: E402

DOWN = "\U0001F44E"


class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        from gateway.platforms.base import SendResult
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="901")


def _normalizer():
    """The real Telegram adapter's reaction normaliser, without constructing an adapter."""
    try:
        from plugins.platforms.telegram.adapter import TelegramAdapter
    except Exception as exc:  # python-telegram-bot missing in this venv
        pytest.skip(f"Telegram adapter not importable: {exc}")
    stub = types.SimpleNamespace(_is_id_like=TelegramAdapter._is_id_like)
    return lambda update: TelegramAdapter._normalize_reaction_event(stub, update)


def _update(chat_id, message_id, emojis):
    mr = types.SimpleNamespace(
        chat=types.SimpleNamespace(id=chat_id), message_id=message_id,
        new_reaction=[types.SimpleNamespace(emoji=e, custom_emoji_id=None) for e in emojis])
    return types.SimpleNamespace(message_reaction=mr)


def test_AC_3_1_real_hermes_hook_reaches_plugin_and_it_answers(tmp_path, monkeypatch):
    """AC-3.1 (integration): a real gateway_platform_event for a thumbs-down on a reply whose turn used
    weekly-report makes the plugin send a message naming the skill and asking What was wrong?."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import lifecycle
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.run import GatewayRunner

    from gateway.config import Platform

    manager = PluginManager()
    ctx = PluginContext(PluginManifest(name="thumbs-fix", version="0"), manager)
    load_plugin().register(ctx)
    hooks = sys.modules["thumbs_fix_plugin.core.hooks"]  # the copy the plugin registered, not core.hooks
    assert manager.has_hook("gateway_platform_event")
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: manager.has_hook(name))
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: manager.invoke_hook(name, **kw))

    # A logged turn: inbound 100, reply ids after it, skill weekly-report.
    state = hooks.get_turn_log().state
    state.set("turns", {"555": [{"chat_id": "555", "message_id": 100, "skills": ["weekly-report"], "session_id": "s"}]})
    adapter = _FakeAdapter()
    hooks._gateway = types.SimpleNamespace(adapters={Platform.TELEGRAM: adapter})

    event = _normalizer()(_update(555, 150, [DOWN]))
    assert event["payload"]["message_id"] == "150" and event["payload"]["emojis"] == [DOWN]
    runner = types.SimpleNamespace(_is_user_authorized_for_source=lambda source: True)

    async def main():
        await GatewayRunner._handle_gateway_platform_event(runner, event, object())
        await settle(hooks=hooks)

    asyncio.run(main())
    assert len(adapter.sent) == 1
    chat, text = adapter.sent[0]
    assert chat == "555" and "weekly-report" in text and text.endswith("What was wrong?")
    (repair,) = state.get("repairs")["items"]
    assert repair["state"] == "awaiting-answer" and repair["question_message_id"] == "901"


def test_AC_4_1_real_hermes_drops_the_answer_so_no_agent_turn_starts(tmp_path, monkeypatch):
    """AC-4.1 (integration): the REAL pre_gateway_dispatch path, with a real MessageEvent, treats the plugin's
    {"action": "skip"} as a drop (returns None, so no agent turn), stores the answer, and lets a slash command and
    an ordinary message with no repair waiting through unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import lifecycle
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_context import PluginContext
    from hermes_cli.plugins_manifest import PluginManifest
    from gateway.config import Platform
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    manager = PluginManager()
    ctx = PluginContext(PluginManifest(name="thumbs-fix", version="0"), manager)
    load_plugin().register(ctx)
    hooks = sys.modules["thumbs_fix_plugin.core.hooks"]
    assert manager.has_hook("pre_gateway_dispatch")

    async def ainvoke(name, **kw):
        return manager.invoke_hook(name, **kw)
    monkeypatch.setattr(lifecycle, "ainvoke_hook", ainvoke)

    state = hooks.get_turn_log().state
    state.set("turns", {"555": [{"chat_id": "555", "message_id": 100, "skills": ["weekly-report"], "session_id": ""}]})
    adapter = _FakeAdapter()
    hooks._gateway = types.SimpleNamespace(adapters={Platform.TELEGRAM: adapter})
    runner = types.SimpleNamespace()

    def make(text, mid, mtype=MessageType.TEXT):
        src = SessionSource(platform=Platform.TELEGRAM, chat_id="555", chat_type="dm", user_id="555")
        return MessageEvent(text=text, message_type=mtype, source=src, message_id=str(mid), user_id="555"), src

    async def dispatch(text, mid, mtype=MessageType.TEXT):
        ev, src = make(text, mid, mtype)
        return await GatewayRunner._hm_pre_gateway_dispatch_hook(runner, ev, src)

    async def main():
        hooks.on_platform_event(platform="telegram", event_type="reaction", payload={
            "emojis": [DOWN], "chat_id": "555", "message_id": "150", "custom_emoji_ids": [], "thread_id": None})
        await settle(hooks=hooks)
        slash = await dispatch("/feedback", 300, MessageType.COMMAND)
        sticker = await dispatch("", 301, MessageType.STICKER)
        answer = await dispatch("dates should be DD/MM/YYYY", 302)
        later = await dispatch("hello again", 303)
        return slash, sticker, answer, later

    slash, sticker, answer, later = asyncio.run(main())
    assert slash is not None and sticker is None and answer is None and later is not None
    (repair,) = state.get("repairs")["items"]
    assert repair["state"] == "drafting" and repair["feedback"] == "dates should be DD/MM/YYYY"
