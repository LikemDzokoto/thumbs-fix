# Apply through skill_manage after our own approval

The plugin owns the approval step, and on approval calls `skill_manage` with `action: patch` through `ctx.dispatch_tool`, so Hermes' own validation, locks and security scan still run. It checks the file hash first and refuses if the skill changed since the diff, and it copies `SKILL.md` to the plugin data directory first, which makes `/feedback undo` cheap. If the user has `skills.write_approval` on, Hermes stages the edit and the plugin says so rather than claiming it was applied.

Decided by second-order thinking: writing the file directly would be simpler today, and would bypass the checks that keep skills valid.

## Consequences

Skills shipped inside plugins are read-only, so the demo skill is created in the user's own skills folder by `/feedback demo`.
