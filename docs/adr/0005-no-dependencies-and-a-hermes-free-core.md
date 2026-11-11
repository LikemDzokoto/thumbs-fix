# No dependencies and a Hermes-free core

The plugin is plain Python with no runtime dependencies, and all logic lives in `core/`, which imports nothing from Hermes. Hook adapters in `__init__.py` convert Hermes calls into core calls and catch everything. State is held in `ctx.state`, which is profile-scoped, atomic and durable, so a repair survives a restart without a database.

Decided by the pragmatism rule: Python is what Hermes runs, the catalog reviews dependency lists, and a Hermes-free core tests in milliseconds on any machine, including this Windows host where Hermes does not run.

## Considered Options

- `plugin_db()` SQLite: more than a small bounded queue needs.
- Testing only against a real Hermes: slow, and impossible on the host. Real-Hermes checks run in WSL as a separate step.
