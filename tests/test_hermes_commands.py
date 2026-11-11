"""Integration: /feedback demo, approve and undo against the REAL Hermes skill_manage and skill_view, in a temporary
HERMES_HOME (never ~/.hermes). Skipped where Hermes is not installed; scripts/check-hermes.sh runs it in WSL."""
import asyncio
import json
import shutil

import pytest

pytest.importorskip("tools.skill_manager_tool", reason="Hermes is not installed on this host")

from core import commands, hooks  # noqa: E402
from core.turns import MemoryState  # noqa: E402
from helpers import settle  # noqa: E402
from test_drafting import FakeLlm  # noqa: E402
from test_hermes_applying import _hermes  # noqa: E402
from test_linker import Gateway  # noqa: E402
from test_repairs import event, open_question, repairs  # noqa: E402

OLD = "Write every date as MM/DD/YYYY."
NEW = "Write every date as DD/MM/YYYY."


def teardown_function(_):
    hooks.configure(MemoryState())


def test_AC_1_1_real_demo_creates_a_loadable_skill_then_an_edit_is_undone_byte_for_byte(tmp_path, monkeypatch):
    """AC-1.1, AC-6.2, AC-13.1: /feedback demo creates weekly-report through real skill_manage and skill_view loads
    it; an applied edit is undone and the SKILL.md bytes equal the original."""
    home, skill, dispatch = _hermes(tmp_path, monkeypatch)
    shutil.rmtree(skill.parent)  # no weekly-report yet
    state, a = open_question()
    hooks.configure(state, llm=FakeLlm({"old_string": OLD, "new_string": NEW}), dispatch_tool=dispatch,
                    data_dir=tmp_path / "plugin-data")

    assert hooks.feedback_command("demo").startswith("Created")
    view = json.loads(dispatch("skill_view", {"name": "weekly-report"}))
    assert view["success"] and OLD in view["content"] and "SAMPLE DATA" in view["content"]
    assert skill.read_bytes() == commands.DEMO_CONTENT.encode("utf-8")
    assert "already exists" in hooks.feedback_command("demo") and skill.read_bytes() == commands.DEMO_CONTENT.encode()
    original = skill.read_bytes()

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        await settle(lambda: repairs(state) and repairs(state)[0]["state"] != "drafting")
    asyncio.run(main())
    assert repairs(state)[0]["state"] == "awaiting-approval", repairs(state)[0]
    assert "weekly-report" in hooks.feedback_command("") and "awaiting-approval" in hooks.feedback_command("")
    assert "Applied" in hooks.feedback_command("approve")
    assert skill.read_bytes() == original.replace(OLD.encode(), NEW.encode()) != original
    assert hooks.feedback_command("undo").startswith("Undone")
    assert skill.read_bytes() == original
