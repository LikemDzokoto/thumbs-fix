# Draft edits with the user's own model, as one validated replacement

The plugin calls `ctx.llm.complete_structured`, which uses whatever provider and model Hermes is configured with, so there are no keys to manage and no paid dependency. The model returns one `old_string` and `new_string`. The plugin rejects empty, identical or non-matching edits, and computes the diff itself so the person approves what will actually happen.

Decided by inversion: the way this loses is a bad edit on camera, so the model's only power is to propose one replacement that a person reads.

## Considered Options

- Let the agent make the edit in its next turn: the output is not validated, and the edit is applied by the same judgement the user is correcting.
- A fixed model and key: adds setup and an outage risk for no benefit.
- Retries: a second wait with the user watching. A failure says so plainly and the repair stays open.
