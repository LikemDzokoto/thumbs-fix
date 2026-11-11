"""/feedback: list, approve, reject, undo, demo (ticket 8). A fake Hermes skill store; the real one is in
test_hermes_commands.py."""
import json
import shutil
import types

from core import applying, commands, hooks
from core.turns import MemoryState
from test_applying import NEW, OLD, FakeHermes, drafted, repairs
from test_linker import CHAT


def teardown_function(_):
    hooks.configure(MemoryState())


def two_open():
    hooks.configure(MemoryState())
    store = hooks._linker.repairs
    store.create(CHAT, 100, ["weekly-report"])
    store.create(CHAT, 200, ["standup-notes"])
    return store


def source(chat_id):
    return types.SimpleNamespace(chat_id=chat_id)


def test_AC_10_1_list_shows_skill_name_and_state(tmp_path):
    """AC-10.1: one open repair for weekly-report awaiting approval: /feedback lists it with skill and state."""
    drafted(tmp_path)
    out = hooks.feedback_command("")
    assert "weekly-report" in out and "awaiting-approval" in out
    assert hooks.feedback_command("list") == out


def test_AC_10_2_no_open_repairs_says_so_and_changes_nothing():
    """AC-10.2: with no open repairs, /feedback, approve and reject say so."""
    hooks.configure(MemoryState())
    assert hooks.feedback_command("") == commands.NO_LIST
    assert hooks.feedback_command("approve") == commands.NONE_OPEN == hooks.feedback_command("reject")
    assert hooks._linker.repairs.all() == []


def test_AC_10_3_two_open_repairs_apply_nothing_and_ask_which():
    """AC-10.3: two open repairs, approve without a name: nothing applied, both listed, asks by skill name."""
    store = two_open()
    for verb in ("approve", "reject"):
        out = hooks.feedback_command(verb)
        assert "weekly-report" in out and "standup-notes" in out and f"/feedback {verb} standup-notes" in out
    assert [r["state"] for r in store.all()] == ["awaiting-answer", "awaiting-answer"]


def test_two_open_repairs_a_named_reject_acts_on_that_one_only():
    store = two_open()
    assert hooks.feedback_command("reject weekly-report") == applying.REJECTED_TEXT
    assert [r["state"] for r in store.all()] == ["rejected", "awaiting-answer"]
    assert "No open repair for `nope`" in hooks.feedback_command("approve nope")


def test_AC_10_4_unknown_subcommand_lists_the_valid_ones():
    """AC-10.4: /feedback frobnicate lists the valid subcommands and changes nothing."""
    store = two_open()
    out = hooks.feedback_command("frobnicate")
    for s in commands.SUBCOMMANDS:
        assert f"/feedback {s}" in out
    assert [r["state"] for r in store.all()] == ["awaiting-answer", "awaiting-answer"]


def test_AC_6_2_approve_applies_the_one_open_repair(tmp_path):
    """AC-6.2: a diff awaiting approval, /feedback approve: the edit is applied and the repair is applied."""
    state, a, fake = drafted(tmp_path)
    assert "Applied" in hooks.feedback_command("approve")
    assert repairs(state)[0]["state"] == "applied" and NEW.encode() in fake.file.read_bytes()


def test_AC_9_2_a_rejected_repair_cannot_be_approved_later(tmp_path):
    """AC-9.2: after /feedback reject, /feedback approve applies nothing and the repair is not listed as open."""
    state, a, fake = drafted(tmp_path)
    before = fake.file.read_bytes()
    assert hooks.feedback_command("reject") == applying.REJECTED_TEXT
    assert hooks.feedback_command("approve") == commands.NONE_OPEN
    assert hooks.feedback_command("") == commands.NO_LIST
    assert repairs(state)[0]["state"] == "rejected" and fake.file.read_bytes() == before
    assert hooks._applier.approve(repairs(state)[0], "feedback-approve")["ok"] is False  # even called directly


def test_reject_of_a_staged_repair_says_the_edit_is_still_waiting_in_hermes(tmp_path):
    state, a, fake = drafted(tmp_path, mode="staged")
    hooks.feedback_command("approve")
    assert repairs(state)[0]["staged"] is True
    out = hooks.feedback_command("reject")
    assert "/skills" in out and "Nothing changed" not in out
    assert repairs(state)[0]["state"] == "rejected"


def test_reject_reads_the_state_inside_the_lock(tmp_path):
    """A stale copy of the repair (still awaiting approval) must not reject an already applied one."""
    state, a, fake = drafted(tmp_path)
    stale = dict(repairs(state)[0])
    hooks.feedback_command("approve")
    out = hooks._applier.reject(stale)
    assert out["ok"] is False and repairs(state)[0]["state"] == "applied"


def test_AC_13_1_undo_restores_the_exact_pre_repair_bytes_and_queues_the_note(tmp_path):
    """AC-13.1: an applied repair with its backup: /feedback undo puts back the original bytes and confirms."""
    state, a, fake = drafted(tmp_path)
    original = fake.file.read_bytes()
    hooks.feedback_command("approve")
    assert fake.file.read_bytes() != original
    hooks._applier.take_note(CHAT)  # the apply's own note is used up
    out = hooks.feedback_command("undo")
    assert out.startswith("Undone") and fake.file.read_bytes() == original
    assert repairs(state)[0]["undone"] is True
    assert "weekly-report" in hooks._applier.take_note(CHAT) and hooks._applier.take_note(CHAT) is None
    assert hooks.feedback_command("undo") == applying.NOTHING_TO_UNDO  # not twice


def test_undo_gives_back_crlf_bytes_even_if_hermes_writes_different_ones(tmp_path):
    """If skill_manage writes different bytes, undo falls back to writing the backup bytes itself."""
    raw = "---\r\nname: weekly-report\r\n---\r\n# Weekly report\r\n\r\n1. " + OLD + "\r\n"
    state, a, fake = drafted(tmp_path, raw=raw)
    original = fake.file.read_bytes()
    hooks.feedback_command("approve")
    orig_call = fake.__call__

    class Mangling(type(fake)):
        def __call__(self, tool, args, **kw):
            out = orig_call(tool, args, **kw)
            if args.get("content"):
                self.file.write_bytes(args["content"].replace("\r\n", "\n").encode())
            return out
    fake.__class__ = Mangling
    assert hooks.feedback_command("undo").startswith("Undone") and fake.file.read_bytes() == original


def test_AC_13_2_nothing_to_undo_changes_no_file(tmp_path):
    """AC-13.2: no applied repair, or a missing backup: nothing to undo and the file is untouched."""
    state, a, fake = drafted(tmp_path)
    before = fake.file.read_bytes()
    assert hooks.feedback_command("undo") == applying.NOTHING_TO_UNDO  # only awaiting approval
    hooks.feedback_command("approve")
    applied = fake.file.read_bytes()
    shutil.rmtree(tmp_path / "data" / "backups")
    assert hooks.feedback_command("undo") == applying.NOTHING_TO_UNDO
    assert fake.file.read_bytes() == applied != before


def test_undo_with_write_approval_on_stages_and_does_not_mark_undone(tmp_path):
    state, a, fake = drafted(tmp_path)
    hooks.feedback_command("approve")
    fake.mode = "staged"
    out = hooks.feedback_command("undo")
    assert out.startswith("Staged") and "undone" not in repairs(state)[0]


def test_commands_act_only_on_the_callers_chat():
    store = two_open()
    store.create("777", 100, ["other-skill"])
    out = hooks.feedback_command("", source=source(CHAT))
    assert "other-skill" not in out and "weekly-report" in out
    assert "other-skill" in hooks.feedback_command("", source=source("777"))
    assert "other-skill" in hooks.feedback_command("")  # no chat (the CLI): single owner, all chats


def test_the_command_handler_names_source_so_hermes_passes_it():
    import inspect

    from core.guard import guard
    assert "source" in inspect.signature(guard(hooks.feedback_command)).parameters  # as register() wraps it


def test_handlers_return_strings_and_never_raise():
    class Broken:
        def get(self, *a):
            raise OSError("gone")
        set = get
    hooks.configure(Broken())
    for arg in ("", "approve", "reject", "undo", "retry", "demo", "x"):
        assert isinstance(hooks.feedback_command(arg), str)
    assert isinstance(hooks.feedback_command(None), str)


def test_AC_1_1_demo_creates_the_weekly_report_skill_with_the_date_flaw(tmp_path):
    """AC-1.1: /feedback demo creates a weekly-report skill whose rule is MM/DD/YYYY, labelled sample data."""
    fake = FakeHermes(tmp_path)
    fake.file.unlink()
    hooks.configure(MemoryState(), dispatch_tool=fake)
    out = hooks.feedback_command("demo")
    text = fake.file.read_text("utf-8")
    assert out.startswith("Created") and "Write every date as MM/DD/YYYY." in text
    assert text.startswith("---\nname: weekly-report\ndescription: ") and "weekly report" in text.split("---")[1]
    assert "SAMPLE DATA" in text and "${HERMES" not in text
    assert [c[1]["action"] for c in fake.calls if c[0] == "skill_manage"] == ["create"]


def test_demo_refuses_to_overwrite_an_existing_skill(tmp_path):
    fake = FakeHermes(tmp_path)
    hooks.configure(MemoryState(), dispatch_tool=fake)
    before = fake.file.read_bytes()
    assert "already exists" in hooks.feedback_command("demo")
    assert fake.file.read_bytes() == before and not [c for c in fake.calls if c[0] == "skill_manage"]


def test_demo_reports_a_failed_create(tmp_path):
    fake = FakeHermes(tmp_path)
    fake.file.unlink()
    hooks.configure(MemoryState(), dispatch_tool=lambda t, a, **k: fake(t, a) if t == "skill_view"
                    else json.dumps({"success": False, "error": "disk full"}))
    assert "disk full" in hooks.feedback_command("demo")
