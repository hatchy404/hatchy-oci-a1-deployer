#!/usr/bin/env python3
"""HATCHY Oracle Cloud Always Free ARM VM acquisition watcher."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from oci.exceptions import ServiceError

from notify import maybe_notify
from oci_client import (
    ACTIVE_LIFECYCLES,
    OciFacade,
    build_launch_details,
    classify_service_error,
    load_oci_config,
    stable_opc_retry_token,
)
from preflight import load_app_config, run_preflight
from state_manager import StateManager, expand, utc_now_iso

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
SYSTEMD_TIMER = "hatchy-oci-watch.timer"
SYSTEMD_SERVICE = "hatchy-oci-watch.service"


class ProcessLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Another watcher instance is running (lock: {self.lock_path})") from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()} at={utc_now_iso()}\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        return False


def disable_timer() -> bool:
    """Stop retries after success or a hard failure.

    On GitHub Actions this disables the workflow schedule. Locally it disables
    the user systemd timer.
    """
    try:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            workflow = os.environ.get("HATCHY_WORKFLOW", "oci-a1-watch.yml")
            cmd = ["gh", "workflow", "disable", workflow]
            if repo:
                cmd.extend(["--repo", repo])
            completed = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if completed.returncode != 0:
                logger.error("Failed to disable GitHub workflow: %s", completed.stderr.strip())
                return False
            return True
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to disable timer: %s", exc)
        return False


def get_public_ip(facade: OciFacade, compartment_id: str, instance_id: str) -> Optional[str]:
    attachments = facade.list_vnic_attachments(compartment_id, instance_id)
    for att in attachments:
        if getattr(att, "lifecycle_state", "") not in {"ATTACHED", "ATTACHING"}:
            continue
        vnic_id = getattr(att, "vnic_id", None)
        if not vnic_id:
            continue
        vnic = facade.get_vnic(vnic_id)
        public_ip = getattr(vnic, "public_ip", None)
        if public_ip:
            return public_ip
    return None


def get_boot_volume_size(
    facade: OciFacade, ad: str, compartment_id: str, instance_id: str
) -> Optional[float]:
    attachments = facade.list_boot_volume_attachments(ad, compartment_id, instance_id)
    for att in attachments:
        bv_id = getattr(att, "boot_volume_id", None)
        if not bv_id:
            continue
        bv = facade.get_boot_volume(bv_id)
        return float(getattr(bv, "size_in_gbs", 0) or 0)
    return None


def wait_for_running(
    facade: OciFacade,
    instance_id: str,
    *,
    max_attempts: int,
    interval_seconds: int,
) -> Any:
    last = None
    for attempt in range(1, max_attempts + 1):
        last = facade.get_instance(instance_id)
        state = getattr(last, "lifecycle_state", None)
        logger.info("Poll %s/%s: %s -> %s", attempt, max_attempts, instance_id, state)
        if state == "RUNNING":
            return last
        if state in {"TERMINATING", "TERMINATED"}:
            raise RuntimeError(f"Instance entered terminal state: {state}")
        time.sleep(interval_seconds)
    return last


def verify_success(
    facade: OciFacade,
    app_config: dict[str, Any],
    instance: Any,
    compartment_id: str,
    availability_domain: str,
) -> dict[str, Any]:
    shape = getattr(instance, "shape", "")
    cfg = getattr(instance, "shape_config", None)
    ocpus = float(getattr(cfg, "ocpus", 0) or 0) if cfg else 0.0
    memory = float(getattr(cfg, "memory_in_gbs", 0) or 0) if cfg else 0.0
    state = getattr(instance, "lifecycle_state", "")
    public_ip = get_public_ip(facade, compartment_id, instance.id)
    boot_size = get_boot_volume_size(facade, availability_domain, compartment_id, instance.id)

    checks = {
        "lifecycle_running": state == "RUNNING",
        "shape_ok": shape == app_config.get("shape", "VM.Standard.A1.Flex"),
        "ocpus_ok": abs(ocpus - float(app_config.get("ocpus", 2))) < 1e-9,
        "memory_ok": abs(memory - float(app_config.get("memory_in_gbs", 12))) < 1e-9,
        "boot_ok": boot_size is not None
        and abs(boot_size - float(app_config.get("boot_volume_size_in_gbs", 100))) < 1e-9,
        "public_ip_ok": bool(public_ip),
    }
    return {
        "instance_ocid": instance.id,
        "display_name": instance.display_name,
        "lifecycle_state": state,
        "shape": shape,
        "ocpus": ocpus,
        "memory_in_gbs": memory,
        "boot_volume_size_in_gbs": boot_size,
        "public_ip": public_ip,
        "availability_domain": availability_domain,
        "region": app_config.get("region"),
        "checks": checks,
        "verified_at": utc_now_iso(),
        "ssh_command": f"ssh ubuntu@{public_ip}" if public_ip else None,
    }


def hard_stop_timer(state_mgr: StateManager, reason: str) -> None:
    disabled = disable_timer()
    state = state_mgr.load()
    state["timer_disabled"] = bool(disabled)
    state = state_mgr.append_history(state, "timer_disabled", {"reason": reason, "ok": disabled})
    state_mgr.save(state)


def run_once(app_config: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    paths = app_config.get("paths") or {}
    state_mgr = StateManager(
        paths.get("state_dir", "~/.local/state/oci-a1-watcher"),
        paths.get("data_dir", "~/.local/share/oci-a1-watcher"),
    )

    if state_mgr.is_completed():
        result = state_mgr.load_result() or {}
        logger.info("Already completed successfully; refusing to launch again")
        return {
            "status": "SUCCESS",
            "ok": True,
            "message": "Already completed",
            "result": result,
        }

    state = state_mgr.load()
    # If a previous launch left an OCID without completion, re-check instead of relaunching
    if state.get("instance_ocid") and not state.get("completed"):
        logger.warning(
            "Previous instance OCID present (%s); confirming instead of creating",
            state["instance_ocid"],
        )
        return _confirm_existing_launch(app_config, state_mgr, state)

    pre = run_preflight(app_config, require_live=True)
    if not pre.ok:
        status = pre.status
        retryable = False
        # Existing RUNNING hatchy surfaced as SUCCESS from preflight
        if status == "SUCCESS" and pre.details.get("existing_hatchy"):
            existing = pre.details["existing_hatchy"]
            # Treat as success only if RUNNING; otherwise block
            if existing.get("lifecycle_state") == "RUNNING":
                # Enrich and stop
                try:
                    cfg = load_oci_config(
                        app_config.get("oci_config_path", "~/.oci/config"),
                        app_config.get("oci_profile", "DEFAULT"),
                    )
                    facade = OciFacade(cfg)
                    inst = facade.get_instance(existing["id"])
                    verified = verify_success(
                        facade,
                        app_config,
                        inst,
                        pre.details["compartment_id"],
                        getattr(inst, "availability_domain", pre.details.get("availability_domain")),
                    )
                    state_mgr.mark_success(verified)
                    hard_stop_timer(state_mgr, "existing_hatchy_running")
                    maybe_notify(app_config, f"HATCHY already RUNNING: {verified.get('public_ip')}")
                    return {"status": "SUCCESS", "ok": True, "result": verified, "messages": pre.messages}
                except Exception as exc:  # noqa: BLE001
                    state_mgr.mark_attempt(
                        "PENDING_CONFIRMATION",
                        error=str(exc),
                        error_code="EXISTING_CONFIRM_FAILED",
                        retryable=False,
                        instance_ocid=existing["id"],
                    )
                    hard_stop_timer(state_mgr, "existing_confirm_failed")
                    return {
                        "status": "PENDING_CONFIRMATION",
                        "ok": False,
                        "messages": pre.messages + [str(exc)],
                    }

        if status == "WATCHING_CAPACITY":
            retryable = True
        state_mgr.mark_attempt(
            status,
            error="; ".join(pre.messages),
            error_code=status,
            retryable=retryable,
        )
        # Capacity/transient waits keep the timer. Hard blockers stop it.
        if status.startswith("BLOCKED_") or status in {"ERROR", "PENDING_CONFIRMATION"}:
            hard_stop_timer(state_mgr, status)
        return {
            "status": status,
            "ok": False,
            "retryable": retryable,
            "messages": pre.messages,
            "details": _safe_details(pre.details),
        }

    if dry_run:
        state_mgr.mark_attempt(
            "READY",
            error=None,
            error_code=None,
            retryable=None,
        )
        return {
            "status": "READY",
            "ok": True,
            "dry_run": True,
            "messages": pre.messages + ["Dry-run OK; launch skipped"],
            "details": _safe_details(pre.details),
        }

    # Launch exactly once
    cfg = load_oci_config(
        app_config.get("oci_config_path", "~/.oci/config"),
        app_config.get("oci_profile", "DEFAULT"),
    )
    facade = OciFacade(cfg)
    compartment_id = pre.details["compartment_id"]
    ad_name = pre.details["availability_domain"]
    image_id = pre.details["image"]["id"]
    ssh_key = pre.details["ssh_public_key"]
    token = stable_opc_retry_token(
        compartment_id,
        app_config.get("instance_name", "HATCHY"),
        app_config.get("shape", "VM.Standard.A1.Flex"),
        str(app_config.get("ocpus", 2)),
        str(app_config.get("memory_in_gbs", 12)),
    )

    details = build_launch_details(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        display_name=app_config.get("instance_name", "HATCHY"),
        shape=app_config.get("shape", "VM.Standard.A1.Flex"),
        ocpus=float(app_config.get("ocpus", 2)),
        memory_in_gbs=float(app_config.get("memory_in_gbs", 12)),
        image_id=image_id,
        boot_volume_size_in_gbs=int(app_config.get("boot_volume_size_in_gbs", 100)),
        boot_volume_vpus_per_gb=int(app_config.get("boot_volume_vpus_per_gb", 10)),
        subnet_id=app_config["subnet_ocid"],
        ssh_public_key=ssh_key,
        freeform_tags=app_config.get("freeform_tags") or {},
    )

    # Persist token before call for crash safety
    pre_state = state_mgr.load()
    pre_state["opc_retry_token"] = token
    pre_state["status"] = "WATCHING_CAPACITY"
    pre_state = state_mgr.append_history(pre_state, "launch_requested", {"opc_retry_token": token})
    state_mgr.save(pre_state)

    try:
        response = facade.launch_instance(details, opc_retry_token=token)
        instance = response.data
        request_id = getattr(response, "headers", {}).get("opc-request-id") if hasattr(response, "headers") else None
        # Persist OCID immediately
        mid = state_mgr.load()
        mid["instance_ocid"] = instance.id
        mid["request_id"] = request_id
        mid["status"] = "PENDING_CONFIRMATION"
        mid = state_mgr.append_history(
            mid,
            "launch_accepted",
            {"instance_ocid": instance.id, "request_id": request_id},
        )
        state_mgr.save(mid)

        poll = app_config.get("poll") or {}
        final_inst = wait_for_running(
            facade,
            instance.id,
            max_attempts=int(poll.get("max_attempts", 30)),
            interval_seconds=int(poll.get("interval_seconds", 20)),
        )
        if getattr(final_inst, "lifecycle_state", None) != "RUNNING":
            state_mgr.mark_attempt(
                "PENDING_CONFIRMATION",
                error="Instance accepted but not RUNNING before poll timeout",
                error_code="POLL_TIMEOUT",
                retryable=False,
                instance_ocid=instance.id,
                request_id=request_id,
                opc_retry_token=token,
            )
            hard_stop_timer(state_mgr, "poll_timeout")
            return {
                "status": "PENDING_CONFIRMATION",
                "ok": False,
                "instance_ocid": instance.id,
                "message": "Launch accepted; awaiting RUNNING confirmation",
            }

        verified = verify_success(facade, app_config, final_inst, compartment_id, ad_name)
        if not all(verified["checks"].values()):
            state_mgr.mark_attempt(
                "PENDING_CONFIRMATION",
                error=f"Instance RUNNING but verification incomplete: {verified['checks']}",
                error_code="VERIFY_INCOMPLETE",
                retryable=False,
                instance_ocid=instance.id,
                public_ip=verified.get("public_ip"),
            )
            hard_stop_timer(state_mgr, "verify_incomplete")
            return {
                "status": "PENDING_CONFIRMATION",
                "ok": False,
                "result": verified,
            }

        state_mgr.mark_success(verified)
        hard_stop_timer(state_mgr, "success")
        maybe_notify(
            app_config,
            f"HATCHY SUCCESS ip={verified.get('public_ip')} ocid={verified.get('instance_ocid')}",
        )
        return {"status": "SUCCESS", "ok": True, "result": verified}

    except ServiceError as exc:
        info = classify_service_error(exc)
        # Ambiguous: if request may have been accepted, do not retry create
        if info["category"] == "capacity":
            state_mgr.mark_attempt(
                "WATCHING_CAPACITY",
                error=info["message"],
                error_code=info["code"] or "OutOfHostCapacity",
                retryable=True,
                opc_retry_token=token,
            )
            return {
                "status": "WATCHING_CAPACITY",
                "ok": False,
                "retryable": True,
                "error": info,
            }
        if info["category"] in {"rate_limit", "transient"}:
            state_mgr.mark_attempt(
                "WATCHING_CAPACITY",
                error=info["message"],
                error_code=info["code"] or str(info["status"]),
                retryable=True,
                opc_retry_token=token,
            )
            return {
                "status": "WATCHING_CAPACITY",
                "ok": False,
                "retryable": True,
                "error": info,
            }
        if info["category"] in {"auth", "permission"}:
            status = "BLOCKED_AUTH"
        else:
            status = "BLOCKED_CONFIGURATION"
        state_mgr.mark_attempt(
            status,
            error=info["message"],
            error_code=info["code"],
            retryable=False,
            opc_retry_token=token,
        )
        hard_stop_timer(state_mgr, status)
        return {"status": status, "ok": False, "error": info}

    except Exception as exc:  # noqa: BLE001
        # Network timeout / unknown: do NOT recreate; require confirmation
        logger.exception("Launch outcome unclear")
        state_mgr.mark_attempt(
            "PENDING_CONFIRMATION",
            error=str(exc),
            error_code="OUTCOME_UNCLEAR",
            retryable=False,
            opc_retry_token=token,
        )
        hard_stop_timer(state_mgr, "outcome_unclear")
        return {
            "status": "PENDING_CONFIRMATION",
            "ok": False,
            "message": "Launch outcome unclear; timer stopped to prevent duplicates",
            "error": str(exc),
        }


def _confirm_existing_launch(
    app_config: dict[str, Any], state_mgr: StateManager, state: dict[str, Any]
) -> dict[str, Any]:
    instance_id = state["instance_ocid"]
    try:
        cfg = load_oci_config(
            app_config.get("oci_config_path", "~/.oci/config"),
            app_config.get("oci_profile", "DEFAULT"),
        )
        facade = OciFacade(cfg)
        inst = facade.get_instance(instance_id)
        lifecycle = getattr(inst, "lifecycle_state", "")
        if lifecycle == "TERMINATED":
            # Clear stale OCID only if terminated; allow future retries
            cleared = state_mgr.load()
            cleared["instance_ocid"] = None
            cleared["status"] = "WATCHING_CAPACITY"
            cleared = state_mgr.append_history(
                cleared, "stale_instance_cleared", {"instance_ocid": instance_id}
            )
            state_mgr.save(cleared)
            return {
                "status": "WATCHING_CAPACITY",
                "ok": False,
                "message": "Previous instance TERMINATED; will retry next cycle",
            }
        if lifecycle in ACTIVE_LIFECYCLES and lifecycle != "RUNNING":
            state_mgr.mark_attempt(
                "PENDING_CONFIRMATION",
                error=f"Instance still {lifecycle}",
                error_code="STILL_PROVISIONING",
                retryable=False,
                instance_ocid=instance_id,
            )
            # Keep watching via timer only for capacity; for known OCID wait manually
            hard_stop_timer(state_mgr, "still_provisioning")
            return {"status": "PENDING_CONFIRMATION", "ok": False, "instance_ocid": instance_id}

        verified = verify_success(
            facade,
            app_config,
            inst,
            cfg["tenancy"],
            getattr(inst, "availability_domain", ""),
        )
        if lifecycle == "RUNNING" and all(verified["checks"].values()):
            state_mgr.mark_success(verified)
            hard_stop_timer(state_mgr, "confirmed_success")
            return {"status": "SUCCESS", "ok": True, "result": verified}

        state_mgr.mark_attempt(
            "PENDING_CONFIRMATION",
            error=f"Instance present ({lifecycle}) but not fully verified",
            error_code="VERIFY_INCOMPLETE",
            retryable=False,
            instance_ocid=instance_id,
            public_ip=verified.get("public_ip"),
        )
        hard_stop_timer(state_mgr, "verify_incomplete")
        return {"status": "PENDING_CONFIRMATION", "ok": False, "result": verified}
    except Exception as exc:  # noqa: BLE001
        state_mgr.mark_attempt(
            "PENDING_CONFIRMATION",
            error=str(exc),
            error_code="CONFIRM_FAILED",
            retryable=False,
            instance_ocid=instance_id,
        )
        hard_stop_timer(state_mgr, "confirm_failed")
        return {"status": "PENDING_CONFIRMATION", "ok": False, "error": str(exc)}


def _safe_details(details: dict[str, Any]) -> dict[str, Any]:
    out = dict(details)
    out.pop("ssh_public_key", None)
    return out


def print_status(app_config: dict[str, Any]) -> int:
    paths = app_config.get("paths") or {}
    state_mgr = StateManager(
        paths.get("state_dir", "~/.local/state/oci-a1-watcher"),
        paths.get("data_dir", "~/.local/share/oci-a1-watcher"),
    )
    state = state_mgr.load()
    result = state_mgr.load_result()

    timer_active = None
    next_elapse = None
    try:
        show = subprocess.run(
            ["systemctl", "--user", "show", SYSTEMD_TIMER, "-p", "ActiveState", "-p", "NextElapseUSecRealtime", "-p", "LastTriggerUSec"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        props = dict(
            line.split("=", 1) for line in show.stdout.splitlines() if "=" in line
        )
        timer_active = props.get("ActiveState")
        next_elapse = props.get("NextElapseUSecRealtime")
    except Exception:  # noqa: BLE001
        pass

    payload = {
        "status": state.get("status"),
        "completed": state.get("completed"),
        "attempt_count": state.get("attempt_count"),
        "last_run_at": state.get("last_run_at"),
        "last_error": state.get("last_error"),
        "last_error_code": state.get("last_error_code"),
        "instance_ocid": state.get("instance_ocid"),
        "public_ip": state.get("public_ip"),
        "timer_disabled": state.get("timer_disabled"),
        "systemd_timer_active": timer_active,
        "next_elapse": next_elapse,
        "success_result": result,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="HATCHY OCI Always Free ARM watcher")
    parser.add_argument("--config", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not launch")
    parser.add_argument("--once", action="store_true", help="Run one acquisition attempt")
    parser.add_argument("--status", action="store_true", help="Show current status JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    app_config = load_app_config(args.config)

    if args.status or not (args.once or args.dry_run):
        return print_status(app_config)

    lock_path = expand(
        (app_config.get("paths") or {}).get(
            "lock_file", "~/.local/state/oci-a1-watcher/watcher.lock"
        )
    )
    try:
        with ProcessLock(lock_path):
            outcome = run_once(app_config, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(json.dumps({"status": "ERROR", "ok": False, "message": str(exc)}, ensure_ascii=False))
        return 3

    if isinstance(outcome, dict) and "details" in outcome:
        outcome["details"] = _safe_details(outcome.get("details") or {})

    print(json.dumps(outcome, indent=2, ensure_ascii=False, default=str))
    if outcome.get("status") == "SUCCESS" or (outcome.get("ok") and outcome.get("dry_run")):
        return 0
    if outcome.get("status") == "WATCHING_CAPACITY":
        return 10
    if str(outcome.get("status", "")).startswith("BLOCKED_"):
        return 2
    if outcome.get("status") == "PENDING_CONFIRMATION":
        return 4
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
