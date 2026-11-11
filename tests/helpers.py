import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_plugin():
    """Import the repo root as a package, the way Hermes loads a plugin."""
    if "thumbs_fix_plugin" in sys.modules:
        return sys.modules["thumbs_fix_plugin"]
    spec = importlib.util.spec_from_file_location(
        "thumbs_fix_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["thumbs_fix_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


async def settle(cond=None, timeout=20.0, hooks=None):
    """Wait for the plugin's scheduled work instead of sleeping a fixed time: until hooks._tasks is empty (tasks
    spawned by tasks are in it too) and, when given, cond() is true. `hooks` is the module whose tasks to watch (default core.hooks). Raises on timeout so a hang is a failure."""
    import asyncio
    import time

    if hooks is None:
        from core import hooks
    deadline = time.monotonic() + timeout
    while True:
        await asyncio.sleep(0)
        pending = [t for t in hooks._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=max(0.0, deadline - time.monotonic()))
        elif cond is None or cond():
            return
        else:
            await asyncio.sleep(0.01)
        if time.monotonic() > deadline:
            raise TimeoutError("scheduled work did not settle")
