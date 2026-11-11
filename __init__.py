"""thumbs-fix Hermes plugin entry point: register(ctx) and guarded hook adapters only."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("thumbs-fix")

FEEDBACK_FALLBACK = "Something went wrong, so nothing was changed."

def register(ctx) -> None:
    """Register hooks and /feedback. Never raises."""
    try:
        # Imported here so pytest can import the repo root as a bare module; Hermes loads it as a package.
        from .core import hooks
        from .core.guard import guard

        try:
            llm = ctx.llm  # PluginLlm facade; needs no config for the user's active model (agent/plugin_llm.py:96)
        except Exception:  # noqa: BLE001
            llm = None
        hooks.configure(getattr(ctx, "state", None), llm=llm, dispatch_tool=getattr(ctx, "dispatch_tool", None))
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            log.warning(
                "thumbs-fix: TELEGRAM_BOT_TOKEN is not set; reactions will not arrive "
                "until the Telegram gateway has a bot token"
            )
        for name, fn in (
            ("pre_gateway_dispatch", hooks.on_inbound),
            ("pre_llm_call", hooks.on_pre_llm),
            ("post_tool_call", hooks.on_tool_call),
            ("gateway_platform_event", hooks.on_platform_event),
        ):
            ctx.register_hook(name, guard(fn, name=name))
        ctx.register_command(
            "feedback",
            guard(hooks.feedback_command, default=FEEDBACK_FALLBACK, name="feedback"),
            description="List, approve, reject or undo skill repairs; demo creates a sample skill",
            args_hint="[approve|reject|undo|demo]",
        )
    except Exception:  # noqa: BLE001
        log.exception("thumbs-fix: register failed")
