# thumbs-fix

A Hermes Agent plugin that turns a Telegram reaction into a consented edit of the skill behind a bad reply.

## Language

**Turn**:
One user message in a private chat and everything the agent did to answer it, identified by the inbound Telegram message id.
_Avoid_: exchange, interaction

**Message id range**:
The ids after a turn's inbound message and before the next inbound message. A reaction inside it belongs to that turn.
_Avoid_: reply id, recency window

**Skill behind a reply**:
A skill the agent loaded with `skill_view` during that turn.
_Avoid_: source skill, responsible skill

**Repair**:
One attempt to fix one skill because of one reaction, from the question through to applied, rejected or failed.
_Avoid_: fix, ticket, feedback item

**Draft**:
The model's proposed replacement, an `old_string` and `new_string` in one `SKILL.md`, not yet approved.
_Avoid_: patch, suggestion

**Diff**:
The text a person reads and approves, computed by the plugin from the draft, never written by the model.
_Avoid_: preview

**Own message**:
A message the plugin itself sent, such as the question, the diff or a confirmation.
_Avoid_: system message, bot reply

**Bot reply**:
A message the agent sent in answer to a turn. Only these can be thumbs-downed into a repair.
_Avoid_: response

**Refusal**:
The polite message saying the plugin cannot tell which skill made this and nothing changed.
_Avoid_: error, failure
