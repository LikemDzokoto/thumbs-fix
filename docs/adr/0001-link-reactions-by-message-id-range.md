# Link reactions to turns by message id range

Hermes tells a plugin the reacted `message_id` but exposes no id for the messages the agent sends, and deliberately ships no "message delivered" hook. In a private Telegram chat both sides share one id sequence, so a reaction belongs to the turn whose inbound message most recently precedes it. We link that way, in private chats only, and refuse anything else.

Decided by first principles: the flow needs "which turn was this", and the id sequence answers that without any access Hermes withholds.

## Considered Options

- Recency window: unnecessary, since reactions always carry an id, and it guesses.
- Wrapping the Telegram adapter's send path to capture reply ids: exact, but it is the core override the catalog's `no core override` check refuses.
- Raw update access through a platform handler: reactions are already normalised for us, and it would not reveal outbound ids.

## Consequences

Groups are out, because their sequence is shared across people. A message sent mid-turn skips the inbound hook and can merge two turns; this is a known Hermes gap and is documented.
