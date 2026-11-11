# Live setup: thumbs-fix on a real Telegram bot (WSL)

Operator runbook. Everything runs inside WSL Ubuntu-24.04; the Windows host install of Hermes is never touched.
Secrets go into `~/.hermes/.env` by hand, in your own terminal. Never paste a bot token or LLM key into chat,
a ticket, a commit or a screenshot.

Open a WSL shell first: `wsl -d Ubuntu-24.04`

## 0. What is already installed (done by ticket 2)

| Thing | Where (inside WSL) |
|---|---|
| Hermes source, pinned commit `333622abbc1a255eea7748e81bbaa2109b692ba1` | `~/hermes-thumbs/src` |
| Python 3.14 virtualenv with Hermes (editable) and the Telegram extra | `~/hermes-thumbs/venv` |
| Hermes home (config, plugins, `.env`) | `~/.hermes` |

Activate it in any shell: `source ~/hermes-thumbs/venv/bin/activate` (then `hermes --version` works).

### Rebuilding the venv from scratch (only if it is lost)

Hermes needs Python 3.14: its `pyproject.toml` marks every core dependency for 3.14, and the gateway runs its
chat sessions on a 3.14 runtime it downloads into `~/.hermes/tools/`. Build the venv on that same interpreter.
A venv on an older Python starts, but the gateway then fails with
`cannot import name 'ArgsKwargs' from 'pydantic_core'` and `hermes chat` reports
`Gateway connection failed; no local fallback (InvalidStatus)`.

```bash
mkdir -p ~/hermes-thumbs && cd ~/hermes-thumbs
git clone https://github.com/NousResearch/hermes-agent.git src
git -C src checkout 333622abbc1a255eea7748e81bbaa2109b692ba1
PY=$(ls -d ~/.hermes/tools/python-3.14*/bin/python3 | head -1)   # present after Hermes has run once
"$PY" -m venv venv && source venv/bin/activate
python -m pip install -U pip
# core + Telegram dependencies from Hermes' own pyproject, skipping browser-harness
# (its Pillow pin conflicts with Hermes' own; thumbs-fix does not use browser tools)
python - <<'EOF'
import tomllib, pathlib
p = tomllib.loads(pathlib.Path("src/pyproject.toml").read_text())["project"]
deps = p["dependencies"] + p["optional-dependencies"]["messaging"]
keep = [d for d in deps if not d.split(";")[0].strip().lower().startswith(("browser-harness", "hermes-agent"))]
pathlib.Path("reqs-314.txt").write_text("\n".join(keep) + "\n")
EOF
python -m pip install -r reqs-314.txt pytest
python -m pip install --no-deps -e ./src
```

If `~/.hermes/tools/` has no Python 3.14 yet, run Hermes' own installer once, or install a 3.14 with
[uv](https://docs.astral.sh/uv/) (`uv python install 3.14`) and use that interpreter.

## 1. Check the plugin against real Hermes (any time)

```bash
bash <path-to-this-repo>/scripts/check-hermes.sh   # or pass another tree as argument
```

Runs `hermes plugins doctor . --ci` and `hermes plugins validate . --install-deps`; non-zero exit on failure.
Adjust the path to wherever the plugin tree you want to test lives (the ticket worktree, or the main checkout
after merge).

## 2. Create the Telegram bot (BotFather)

1. In Telegram, open `@BotFather` and send `/newbot`.
2. Pick a display name and a unique username ending in `bot`.
3. BotFather replies with a token like `123456789:AA...`. Keep it private; if it leaks, send `/revoke` to rotate it.
4. Reactions work in private chats with the bot; nothing else needs enabling. (For group use, make the bot an
   admin or disable privacy mode with `/setprivacy` so it sees messages and reactions.)

## 3. Find your own Telegram user id

Message `@userinfobot`; it replies with a numeric id (not your @username). That number is what the gateway
allowlist wants.

## 4. Put the secrets in `~/.hermes/.env` by hand

```bash
mkdir -p ~/.hermes
nano ~/.hermes/.env        # create or edit it yourself
chmod 600 ~/.hermes/.env
```

Add these lines, with your real values typed in the editor:

```
# Telegram bot token from BotFather
TELEGRAM_BOT_TOKEN=<your token>

# Who may talk to the bot: comma-separated numeric Telegram user ids. Without this the gateway
# denies everyone (or answers strangers with a pairing code).
TELEGRAM_ALLOWED_USERS=<your numeric id>

# The key for your LLM provider: the variable name depends on the provider, see section 5.
```

`TELEGRAM_ALLOWED_USERS` is the exact key Hermes reads (platform-wide allowlist; see
`gateway/authz_mixin.py` and the "team Telegram assistant" guide in the Hermes docs). Do not set
`GATEWAY_ALLOW_ALL_USERS=true`; the allowlist is the point.

## 5. Choose the LLM provider (generic)

Hermes needs one model provider; the plugin drafts skill edits with whichever model you configure (ADR 3), so
it needs nothing extra. Configure it with the interactive picker:

```bash
source ~/hermes-thumbs/venv/bin/activate
hermes model
```

It saves the choice to `~/.hermes/config.yaml` (the `model:` block: `provider`, `default`/model name) and asks
for the provider's key or runs its login (OAuth providers need no key). API-key providers expect the key in
`~/.hermes/.env` under that provider's variable name (the picker tells you which); add it by hand as in
section 4 if you prefer to skip the prompt. Check afterwards with `hermes chat -q "say hi"` before touching
Telegram.

## 6. Install and enable the plugin from the local tree

Plugins are disabled by default. `hermes plugins install` clones from git; for a local working tree, link the
directory into the user plugin folder (the folder name must equal the manifest `name`, `thumbs-fix`), so edits
show up without reinstalling:

```bash
source ~/hermes-thumbs/venv/bin/activate
mkdir -p ~/.hermes/plugins
ln -s <path-to-this-repo> ~/.hermes/plugins/thumbs-fix   # e.g. a /mnt/c/... path in WSL
hermes plugins list            # thumbs-fix should appear, not yet enabled
hermes plugins enable thumbs-fix
```

(If you merged to a different checkout, point the link there. If the link ever causes trouble on `/mnt/c`,
copy instead: `cp -r <tree> ~/.hermes/plugins/thumbs-fix` and re-copy after changes.) Restart the gateway after
enabling or changing the plugin.

## 7. Start the gateway (your step)

Foreground first, so you see errors:

```bash
source ~/hermes-thumbs/venv/bin/activate
hermes gateway
```

You should see the Telegram adapter connect. Send the bot a message from your allowed account and get a reply.
Then react with a thumbs-down to one of its replies. Stop with Ctrl+C. For a background service afterwards:
`hermes gateway install` then `hermes gateway start` / `status` / `stop`.

## Troubleshooting

- Bot silent: the id in `TELEGRAM_ALLOWED_USERS` is wrong or the gateway was not restarted after editing `.env`.
- Plugin not loading: `hermes plugins list` must show `thumbs-fix` enabled; run `scripts/check-hermes.sh`.
- Token leaked: `/revoke` in BotFather, update `.env`, restart the gateway.

## The model that drafts the edit (ticket 6)

thumbs-fix drafts each edit with `ctx.llm.complete_structured`, which uses the model Hermes is already configured with
(`hermes setup`, `model.provider` / `model.default` in `~/.hermes/config.yaml`). No key or setting is needed for the
plugin: it passes no `provider`, `model`, `agent_id`, `profile` or `task` override, and only those are gated by
`plugins.entries.thumbs-fix.llm.allow_*_override` (agent/plugin_llm.py:121-176). If Hermes has no usable model, or the
call fails or takes more than 30 s, the bot says it could not draft a fix and the skill stays untouched; send
`/feedback retry` to try again.

Live check (needs a configured model): `python -m pytest -q -m live tests/test_drafting_live.py` in the Hermes venv.
