"""Ticket 10: the README disclosure, the catalog entry and the manifest agree with each other and with register().

The real `hermes plugins validate` and `doctor` runs are in scripts/check-hermes.sh (run for AC-11.3; not part of the GitHub Actions run);
these tests are the part that runs anywhere, including the GitHub Actions run.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_KEYS = ("name", "repo", "sha", "description", "maintainer", "tier", "category", "requires_hermes", "capabilities")


def _load(rel):
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((ROOT / rel).read_text(encoding="utf-8"))


def test_AC_11_3_catalog_entry_parses_and_declares_what_the_manifest_declares():
    """AC-11.3: the catalog entry has the required keys, a sha placeholder, and capabilities equal to plugin.yaml,
    so the capabilities check of `hermes plugins validate` (scripts/check-hermes.sh) has nothing to disagree with."""
    entry, manifest = _load("docs/catalog/thumbs-fix.yaml"), _load("plugin.yaml")
    for key in REQUIRED_KEYS:
        assert entry.get(key), key
    assert entry["name"] == manifest["name"] == "thumbs-fix"
    assert re.fullmatch(r"[a-z0-9_-]{1,64}", entry["name"])
    assert entry["tier"] == "community" and entry["category"] == "tools"
    assert entry["repo"].startswith("https://")
    assert entry["requires_hermes"] == manifest["requires_hermes"] == ">=0.21.5"
    assert str(entry["version"]) == str(manifest["version"])
    assert not re.fullmatch(r"[0-9a-f]{40}", entry["sha"]), "the sha is pinned by the operator, see docs/submit.md"
    caps = entry["capabilities"]
    assert sorted(caps["provides_hooks"]) == sorted(manifest["provides_hooks"])
    assert caps["provides_tools"] == [] and caps["provides_middleware"] == [] and caps["requires_env"] == []


def test_AC_11_3_manifest_hooks_are_the_hooks_register_installs():
    """AC-11.3: register(ctx) registers exactly the hooks plugin.yaml declares, plus the /feedback command."""
    from helpers import load_plugin
    mod = load_plugin()

    class Ctx:
        state = None
        llm = None
        dispatch_tool = None

        def __init__(self):
            self.hooks, self.commands = [], []

        def register_hook(self, name, fn):
            self.hooks.append(name)

        def register_command(self, name, fn, **kw):
            self.commands.append(name)

    ctx = Ctx()
    mod.register(ctx)
    assert sorted(ctx.hooks) == sorted(_load("plugin.yaml")["provides_hooks"])
    assert ctx.commands == ["feedback"]


def test_AC_1_1_readme_has_install_disclosure_limits_and_the_demo():
    """AC-1.1: the judge-facing README carries the install line, the disclosure sections, the limits and the 7-step demo."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for heading in ("## Install", "## Telegram setup", "## Known limits", "## Disclosure",
                    "## How this differs from reaction-feedback and reaction-ledger", "## The demo in 7 steps", "## Licence"):
        assert heading in text, heading
    assert "hermes plugins install LikemDzokoto/thumbs-fix" in text and "hermes plugins enable thumbs-fix" in text
    assert "OWNER" not in text
    disclosure = text.split("## Disclosure", 1)[1].split("\n## ", 1)[0]
    for hook in ("pre_gateway_dispatch", "pre_llm_call", "post_tool_call", "gateway_platform_event"):
        assert hook in disclosure, hook
    for phrase in ("ctx.state", "backups", "only after your explicit approval", "your own model"):
        assert phrase in disclosure, phrase
    demo = text.split("## The demo in 7 steps", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"^\d\. ", demo, flags=re.M)) == 7
    assert "/feedback demo" in demo


def test_AC_1_1_license_is_mit_and_submit_steps_exist():
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
    submit = (ROOT / "docs" / "submit.md").read_text(encoding="utf-8")
    for needle in ("plugin-catalog/thumbs-fix.yaml", "NousResearch/hermes-agent", "rev-parse HEAD", "@NousResearch"):
        assert needle in submit, needle
