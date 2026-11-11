import ast
import importlib
import json
import logging
import re
import sys

from helpers import ROOT, load_plugin


class FakeCtx:
    def __init__(self):
        self.hooks = {}
        self.commands = {}

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_command(self, name, handler, description="", args_hint="", **kw):
        self.commands[name] = handler


def manifest():
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    top, hooks, cur = {}, [], None
    for line in text.splitlines():
        if re.match(r"^\s+- ", line) and cur == "provides_hooks":
            hooks.append(line.split("- ", 1)[1].strip())
        elif ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            cur = k.strip()
            top[cur] = v.strip().strip('"')
    top["provides_hooks"] = hooks
    return top


def test_AC_1_2_manifest_matches_registration():
    """AC-1.2: Given the plugin.yaml, when its manifest is compared with what the plugin registers,
    then provides_hooks lists exactly the registered hooks, no elevated capability is declared,
    requires_env is empty and requires_hermes is >=0.21.5."""
    m = manifest()
    ctx = FakeCtx()
    load_plugin().register(ctx)
    assert m["manifest_version"] == "2"
    assert m["name"] == "thumbs-fix"
    assert sorted(m["provides_hooks"]) == sorted(ctx.hooks)
    assert sorted(ctx.hooks) == sorted(
        ["pre_gateway_dispatch", "pre_llm_call", "post_tool_call", "gateway_platform_event"]
    )
    assert "feedback" in ctx.commands
    assert m["requires_hermes"] == ">=0.21.5"
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    keys = {line.split(":", 1)[0].strip() for line in text.splitlines() if ":" in line}
    assert not keys & {"requires_env", "platform_actions", "llm", "capabilities", "gateway"}


def test_core_imports_nothing_from_hermes():
    """core/ may import only the standard library, __future__ and its own relative imports."""
    bad = []
    for f in (ROOT / "core").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    continue
                mods = [node.module or ""]
            else:
                continue
            bad += [(f.name, m) for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
    assert bad == []


def test_guard_catches_raising_core_function(caplog):
    from core.guard import guard

    def boom(**kw):
        raise RuntimeError("core exploded")

    with caplog.at_level(logging.ERROR, logger="thumbs-fix"):
        assert guard(boom, default="safe", name="boom")(a=1) == "safe"
    assert "boom failed" in caplog.text


def test_registered_adapters_swallow_core_errors(monkeypatch):
    plugin = load_plugin()
    hooks = importlib.import_module("thumbs_fix_plugin.core.hooks")  # self-contained: no reliance on test order

    def boom(**kw):
        raise ValueError("x")

    monkeypatch.setattr(hooks, "on_pre_llm", boom)  # patched before register() captures it
    ctx = FakeCtx()
    plugin.register(ctx)
    assert ctx.hooks["pre_llm_call"](session_id="s", extra=1) is None


def test_AC_1_3_no_token_logs_and_does_not_raise(monkeypatch, caplog):
    """AC-1.3: Given no Telegram bot token, when the plugin is loaded, then a log line names the
    missing token and no exception is raised."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ctx = FakeCtx()
    with caplog.at_level(logging.WARNING, logger="thumbs-fix"):
        load_plugin().register(ctx)
    assert "TELEGRAM_BOT_TOKEN" in caplog.text
    assert len(ctx.hooks) == 4


def test_register_never_raises_when_core_import_fails(monkeypatch):
    """The lazy imports sit inside register()'s try, so an ImportError is logged, not raised."""
    plugin = load_plugin()
    monkeypatch.setitem(sys.modules, "thumbs_fix_plugin.core.hooks", None)  # makes the import raise ImportError
    plugin.register(FakeCtx())


def test_register_survives_a_broken_ctx():
    class Broken:
        def register_hook(self, *a, **k):
            raise RuntimeError("nope")

    load_plugin().register(Broken())


def test_feedback_command_returns_text():
    ctx = FakeCtx()
    load_plugin().register(ctx)
    assert ctx.commands["feedback"]("") == "No open repairs."


def test_license_is_mit():
    t = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert t.startswith("MIT License") and "Likem Dzokoto" in t
