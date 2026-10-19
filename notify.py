#!/usr/bin/env python3
"""Optional notification helper. Only runs an approved local command if configured."""

from __future__ import annotations

import logging
import shlex
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def maybe_notify(config: dict[str, Any], message: str) -> bool:
    """Send notification only when explicitly enabled with a local command.

    Returns True only if a command was actually executed successfully.
    Never claims delivery when notify is disabled.
    """
    notify_cfg = config.get("notify") or {}
    if not notify_cfg.get("enabled"):
        logger.info("Notify disabled; message retained in logs/state only: %s", message)
        return False

    command = notify_cfg.get("command")
    if not command:
        logger.warning("Notify enabled but no command configured")
        return False

    try:
        if isinstance(command, str):
            args = shlex.split(command)
        else:
            args = list(command)
        completed = subprocess.run(
            args + [message],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            logger.error("Notify command failed: %s", completed.stderr.strip())
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Notify exception: %s", exc)
        return False
