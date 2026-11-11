"""Integration with Hermes' real ctx.llm facade (agent/plugin_llm.py): our call passes the real trust gate with no
config, the real schema parser hands back .parsed, and the draft validates. The provider call itself is injected.
Skipped where Hermes is not installed; scripts/check-hermes.sh runs it in the WSL venv.
"""
import json
import types

import pytest

pytest.importorskip("agent.plugin_llm", reason="Hermes is not installed on this host")

from agent.plugin_llm import PluginLlmTrustError, _TrustPolicy, make_plugin_llm_for_test  # noqa: E402

from core import drafting  # noqa: E402

SKILL = "# S\n\n1. Format every date as MM/DD/YYYY.\n2. Be brief.\n"


def _caller(reply, seen):
    def call(**kw):
        seen.append(kw)
        msg = types.SimpleNamespace(content=reply)
        resp = types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)], usage=None, model="test-model")
        return "test-provider", "test-model", resp
    return call


def test_restrictive_default_policy_accepts_our_call_and_parses_the_draft():
    seen = []
    reply = json.dumps({"old_string": "MM/DD/YYYY", "new_string": "DD/MM/YYYY"})
    llm = make_plugin_llm_for_test(plugin_id="thumbs-fix", policy=_TrustPolicy(plugin_id="thumbs-fix"),
                                   sync_caller=_caller("```json\n" + reply + "\n```", seen))
    prompt = drafting.build_prompt("s", SKILL, "dates should be DD/MM/YYYY")
    result = llm.complete_structured(**drafting.complete_kwargs(prompt))
    assert result.content_type == "json" and result.parsed["new_string"] == "DD/MM/YYYY"
    (kw,) = seen
    assert kw["max_tokens"] == 800 and kw["timeout"] == 30 and kw["provider_override"] is None and kw["model_override"] is None
    roles = [m["role"] for m in kw["messages"]]
    assert roles == ["system", "user"]
    assert "dates should be DD/MM/YYYY" not in kw["messages"][0]["content"] and "Format every date" not in kw["messages"][0]["content"]
    assert "old_string" in kw["messages"][1]["content"][0]["text"]  # the JSON schema travels in the user header
    assert isinstance(drafting.validate("s", SKILL, result), drafting.Draft)


def test_schema_violation_raises_so_the_flow_says_could_not_draft():
    llm = make_plugin_llm_for_test(plugin_id="thumbs-fix", policy=_TrustPolicy(plugin_id="thumbs-fix"),
                                   sync_caller=_caller(json.dumps({"old_string": 3}), []))
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        pytest.skip("jsonschema not installed in this Hermes venv; the plugin validates the draft itself anyway")
    with pytest.raises(ValueError):
        llm.complete_structured(**drafting.complete_kwargs(drafting.build_prompt("s", SKILL, "x")))


def test_a_provider_override_would_be_refused_which_is_why_we_never_pass_one():
    llm = make_plugin_llm_for_test(plugin_id="thumbs-fix", policy=_TrustPolicy(plugin_id="thumbs-fix"),
                                   sync_caller=_caller("{}", []))
    with pytest.raises(PluginLlmTrustError):
        llm.complete_structured(**drafting.complete_kwargs(drafting.build_prompt("s", SKILL, "x")), provider="openai")
