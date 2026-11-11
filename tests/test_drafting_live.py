"""Live: one real model call through Hermes' ctx.llm with the user's configured model. Deselected by default;
run with `python -m pytest -q -m live` inside the Hermes venv, with a model configured (hermes setup).
Skips only when Hermes is not installed or no model is configured; any other error fails the test.
"""
import pytest

from core import drafting

SKILL = ("---\nname: weekly-report\ndescription: Write the weekly status report\n---\n# Weekly report\n\n"
         "1. Collect the week's merged pull requests.\n2. Format every date as MM/DD/YYYY, for example 11/09/2026.\n"
         "3. Group items under Shipped, In progress and Blocked.\n")


@pytest.mark.live
def test_live_model_drafts_a_valid_minimal_edit():
    """AC-5.1 (live): a real model, given the feedback, proposes one valid replacement and the plugin renders its diff."""
    pytest.importorskip("agent.plugin_llm", reason="Hermes is not installed here")
    from agent.plugin_llm import PluginLlm
    import agent.auxiliary_client as ac
    if not (ac._read_main_model() or "").strip():  # agent/plugin_llm.py:440 reads the model the same way
        pytest.skip("no model is configured (run hermes setup)")
    llm = PluginLlm(plugin_id="thumbs-fix")
    prompt = drafting.build_prompt("weekly-report", SKILL, "dates should be DD/MM/YYYY")
    result = llm.complete_structured(**drafting.complete_kwargs(prompt))  # a real error fails the test
    draft = drafting.validate("weekly-report", SKILL, result)
    assert isinstance(draft, drafting.Draft), draft
    assert "DD/MM/YYYY" in draft.new_string and drafting.render_messages(draft.diff)
