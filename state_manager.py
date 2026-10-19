#!/usr/bin/env python3
"""Persistent state management for HATCHY OCI A1 watcher."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_STATUSES = {
    "READY",
    "WATCHING_CAPACITY",
    "BLOCKED_AUTH",
    "BLOCKED_NETWORK",
    "BLOCKED_FREE_TIER",
    "BLOCKED_CONFIGURATION",
    "PENDING_CONFIRMATION",
    "SUCCESS",
    "ERROR",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expand(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


class StateManager:
    def __init__(self, state_dir: str | Path, data_dir: str | Path):
        self.state_dir = expand(state_dir)
        self.data_dir = expand(data_dir)
        self.state_file = self.state_dir / "state.json"
        self.result_file = self.data_dir / "success_result.json"
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        os.chmod(self.data_dir, 0o700)

    def default_state(self) -> dict[str, Any]:
        return {
            "status": "READY",
            "attempt_count": 0,
            "last_run_at": None,
            "last_error": None,
            "last_error_code": None,
            "last_retryable": None,
            "instance_ocid": None,
            "opc_retry_token": None,
            "request_id": None,
            "public_ip": None,
            "completed": False,
            "timer_disabled": False,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "history": [],
        }

    def load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            state = self.default_state()
            self.save(state)
            return state
        with self.state_file.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        base = self.default_state()
        base.update(data)
        return base

    def save(self, state: dict[str, Any]) -> None:
        state = dict(state)
        state["updated_at"] = utc_now_iso()
        status = state.get("status")
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        fd, tmp_name = tempfile.mkstemp(dir=str(self.state_dir), prefix=".state.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.state_file)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def append_history(self, state: dict[str, Any], event: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        history = list(state.get("history") or [])
        history.append(
            {
                "at": utc_now_iso(),
                "event": event,
                "detail": detail or {},
            }
        )
        # Keep last 100 events
        state["history"] = history[-100:]
        return state

    def mark_attempt(
        self,
        status: str,
        error: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        state = self.load()
        state["status"] = status
        state["attempt_count"] = int(state.get("attempt_count") or 0) + 1
        state["last_run_at"] = utc_now_iso()
        state["last_error"] = error
        state["last_error_code"] = error_code
        state["last_retryable"] = retryable
        for key, value in extra.items():
            state[key] = value
        state = self.append_history(
            state,
            "attempt",
            {
                "status": status,
                "error": error,
                "error_code": error_code,
                "retryable": retryable,
            },
        )
        self.save(state)
        return state

    def mark_success(self, result: dict[str, Any]) -> dict[str, Any]:
        state = self.load()
        state["status"] = "SUCCESS"
        state["completed"] = True
        state["timer_disabled"] = True
        state["last_run_at"] = utc_now_iso()
        state["last_error"] = None
        state["last_error_code"] = None
        state["last_retryable"] = False
        state["instance_ocid"] = result.get("instance_ocid")
        state["public_ip"] = result.get("public_ip")
        state = self.append_history(state, "success", result)
        self.save(state)

        fd, tmp_name = tempfile.mkstemp(dir=str(self.data_dir), prefix=".result.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.result_file)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return state

    def is_completed(self) -> bool:
        state = self.load()
        return bool(state.get("completed")) or state.get("status") == "SUCCESS"

    def load_result(self) -> dict[str, Any] | None:
        if not self.result_file.exists():
            return None
        with self.result_file.open("r", encoding="utf-8") as fh:
            return json.load(fh)
