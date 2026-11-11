"""Integration: apply through the REAL Hermes skill_view and skill_manage on a temporary skill in a temporary
HERMES_HOME (never ~/.hermes). Skipped where Hermes is not installed; scripts/check-hermes.sh runs it in WSL.

Real: tools.skills_tool.skill_view (the content hash and skill_dir), tools.skill_manager_tool.skill_manage patch
(fuzzy matching, frontmatter validation, atomic write), the write_approval gate, and the ledger. Fake: Telegram.
"""
import importlib
import json
import sys

import pytest

pytest.importorskip("tools.skill_manager_tool", reason="Hermes is not installed on this host")

from core import applying, hooks  # noqa: E402
from helpers import settle  # noqa: E402
from core.turns import MemoryState  # noqa: E402
from test_applying import NEW, OLD, RAW, diff_id  # noqa: E402,F401
from test_drafting import good  # noqa: E402
from test_linker import CHAT, UP, react, run  # noqa: E402
from test_repairs import event, open_question, repairs  # noqa: E402

RAW_REAL = RAW.replace("name: weekly-report\n", "name: weekly-report\ndescription: Write the weekly status report\n")


async def _settled(state):
    """Wait (up to 15 s) for the drafting task: the first real skill_view imports a lot."""
    await settle(lambda: repairs(state) and repairs(state)[0]["state"] != "drafting")


def _hermes(tmp_path, monkeypatch, write_approval=False):
    home = tmp_path / "hermes-home"
    (home / "skills" / "weekly-report").mkdir(parents=True)
    skill = home / "skills" / "weekly-report" / "SKILL.md"
    skill.write_bytes(RAW_REAL.encode("utf-8"))
    if write_approval:
        (home / "config.yaml").write_text("skills:\n  write_approval: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for m in [m for m in sys.modules if m.startswith(("hermes_constants",))]:
        importlib.reload(sys.modules[m])
    import tools.skill_manager_tool  # noqa: F401  registers skill_manage
    import tools.skills_tool  # noqa: F401  registers skill_view
    from tools.registry import registry

    def dispatch(tool, args, **kw):
        return registry.dispatch(tool, args)
    return home, skill, dispatch


def _draft(tmp_path, dispatch):
    import asyncio
    from test_linker import Gateway
    state, a = open_question()
    hooks.configure(state, llm=good(), dispatch_tool=dispatch, data_dir=tmp_path / "plugin-data")

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("dates should be DD/MM/YYYY"), gateway=hooks._gateway)
        await _settled(state)
    asyncio.run(main())
    return state, a


def teardown_function(_):
    hooks.configure(MemoryState())


def test_AC_7_1_real_skill_manage_applies_exactly_the_diffed_lines_with_a_backup(tmp_path, monkeypatch):
    """AC-6.1, AC-7.1, AC-7.2: with the real skill_view and skill_manage, a thumbs-up changes the real SKILL.md by
    exactly the diffed line, the backup holds the previous raw bytes, and the next pre_llm_call tells the agent."""
    home, skill, dispatch = _hermes(tmp_path, monkeypatch)
    state, a = _draft(tmp_path, dispatch)
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval", (r.get("draft_error"), a.sent)
    before = skill.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "applied", (r, a.sent[-1])
    after = skill.read_bytes()
    from pathlib import Path
    assert Path(r["backup_path"]).read_bytes() == before
    assert Path(r["backup_path"]).is_relative_to(tmp_path / "plugin-data")
    assert after == before.replace(OLD.encode(), NEW.encode())
    old, new = before.decode().splitlines(), after.decode().splitlines()
    assert [(o, n) for o, n in zip(old, new) if o != n] == [("2. " + OLD, "2. " + NEW)] and len(old) == len(new)
    note = hooks.on_pre_llm(session_id="s1", platform="telegram", sender_id=CHAT)
    assert "weekly-report" in note["context"] and hooks.on_pre_llm(session_id="s1", platform="telegram", sender_id=CHAT) is None


def test_AC_6_4_real_skill_view_hash_survives_template_vars_and_detects_a_change(tmp_path, monkeypatch):
    """AC-6.4: the stored hash is of real skill_view content (HERMES_SKILL_DIR expanded), so it matches at approval
    time for an unchanged skill, and a changed skill is refused."""
    home, skill, dispatch = _hermes(tmp_path, monkeypatch)
    skill.write_bytes((RAW_REAL + "Templates: ${HERMES_SKILL_DIR}/templates\n").encode())
    state, a = _draft(tmp_path, dispatch)
    assert str(skill.parent) in json.loads(dispatch("skill_view", {"name": "weekly-report"}))["content"]
    skill.write_bytes(skill.read_bytes() + b"Extra line.\n")
    changed = skill.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and skill.read_bytes() == changed and a.sent[-1][1] == applying.STALE


def test_AC_6_4_real_expanded_value_is_refused_and_unexpanded_token_applies(tmp_path, monkeypatch):
    """AC-6.4: real skill_view shows the expanded path; a draft quoting it is refused with the file untouched."""
    from test_drafting import FakeLlm
    home, skill, dispatch = _hermes(tmp_path, monkeypatch)
    skill.write_bytes((RAW_REAL + "Templates: ${HERMES_SKILL_DIR}/templates\n").encode())
    expanded = f"Templates: {skill.parent}/templates"
    import asyncio
    from test_linker import Gateway
    state, a = open_question()
    hooks.configure(state, llm=FakeLlm({"old_string": expanded, "new_string": "Templates: ./templates"}),
                    dispatch_tool=dispatch, data_dir=tmp_path / "plugin-data")

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("fix the path"), gateway=hooks._gateway)
        await _settled(state)
    asyncio.run(main())
    before = skill.read_bytes()
    assert repairs(state)[0]["state"] == "awaiting-approval"
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and skill.read_bytes() == before and a.sent[-1][1] == applying.EXPANDED


def test_AC_7_1_real_write_approval_stages_and_the_bot_says_staged(tmp_path, monkeypatch):
    """AC-7.1: with skills.write_approval on, real skill_manage stages the patch; the plugin reports staged, the
    file is unchanged and the repair is not marked applied."""
    home, skill, dispatch = _hermes(tmp_path, monkeypatch, write_approval=True)
    state, a = _draft(tmp_path, dispatch)
    before = skill.read_bytes()
    run(a, lambda: react(diff_id(state), (UP,)))
    (r,) = repairs(state)
    assert r["state"] == "awaiting-approval" and r["staged"] is True and r["pending_id"], r
    assert a.sent[-1][1].startswith("Staged, not applied") and skill.read_bytes() == before


def test_AC_7_3_real_skill_manage_error_marks_failed_and_leaves_the_file(tmp_path, monkeypatch):
    """AC-7.3: a patch real skill_manage refuses (it would break the frontmatter) marks the repair failed with the
    file byte-identical."""
    from test_drafting import FakeLlm
    import asyncio
    from test_linker import Gateway
    home, skill, dispatch = _hermes(tmp_path, monkeypatch)
    state, a = open_question()
    hooks.configure(state, llm=FakeLlm({"old_string": "---\nname: weekly-report\n", "new_string": ""}),
                    dispatch_tool=dispatch, data_dir=tmp_path / "plugin-data")

    async def main():
        hooks._gateway = Gateway(a)
        hooks.on_inbound(event=event("remove the name"), gateway=hooks._gateway)
        await _settled(state)
    asyncio.run(main())
    before = skill.read_bytes()
    if repairs(state)[0]["state"] != "awaiting-approval":
        pytest.skip("drafting already rejected this edit")
    run(a, lambda: react(diff_id(state), (UP,)))
    assert repairs(state)[0]["state"] == "failed" and skill.read_bytes() == before
    assert "could not be applied" in a.sent[-1][1] and "Nothing changed" in a.sent[-1][1]
