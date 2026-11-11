# Talk to Telegram through the gateway handle

The reaction hook only observes, and `send_message` is deliberately not reachable through `ctx.dispatch_tool`. The plugin keeps the `gateway` handle that `pre_gateway_dispatch` provides and sends with `gateway.adapters[platform].send(...)`, the side-channel the hook documentation describes. `send` returns the message id, so the diff message can be approved by reaction. The same hook returns `skip` for the user's answer, so it never starts an agent turn.

Decided by Pareto: zero setup for the user, and it carries the whole flow.

## Considered Options

- `ctx.platform_actions`: gated off by default behind `gateway.platform_actions`, so every user would need a config change before the plugin works.
- Injecting the question through `pre_llm_call` on the next turn: nothing happens until the user speaks again, which breaks "tap and get asked".

## Consequences

After a gateway restart the plugin has no handle until the user sends one message. Repairs survive, and the limit is documented.
