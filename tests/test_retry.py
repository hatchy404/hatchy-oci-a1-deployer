from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oci.exceptions import ServiceError

import watcher


def _base_config(tmp_path):
    return {
        "instance_name": "HATCHY",
        "region": "ap-tokyo-1",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_in_gbs": 12,
        "boot_volume_size_in_gbs": 100,
        "boot_volume_vpus_per_gb": 10,
        "subnet_ocid": "ocid1.subnet.oc1..sub",
        "vcn_ocid": "ocid1.vcn.oc1..vcn",
        "ssh_public_key_path": str(tmp_path / "id.pub"),
        "oci_config_path": str(tmp_path / "oci_config"),
        "oci_profile": "DEFAULT",
        "freeform_tags": {"managed_by": "hatchy-oci-a1-watcher", "project": "HATCHY"},
        "paths": {
            "state_dir": str(tmp_path / "state"),
            "data_dir": str(tmp_path / "data"),
            "lock_file": str(tmp_path / "state" / "lock"),
        },
        "poll": {"max_attempts": 2, "interval_seconds": 0},
        "notify": {"enabled": False},
    }


def _ok_preflight_details():
    return {
        "compartment_id": "ocid1.tenancy.oc1..ten",
        "availability_domain": "NRTK:AP-TOKYO-1-AD-1",
        "image": {"id": "ocid1.image.oc1..img"},
        "ssh_public_key": "ssh-ed25519 AAAA test",
    }


def test_capacity_error_keeps_watching(tmp_path):
    cfg = _base_config(tmp_path)
    pre = SimpleNamespace(
        ok=True,
        status="READY",
        messages=["ok"],
        details=_ok_preflight_details(),
    )
    facade = MagicMock()
    facade.launch_instance.side_effect = ServiceError(
        status=500,
        code="InternalError",
        headers={},
        message="Out of host capacity for shape VM.Standard.A1.Flex",
    )

    with patch("watcher.run_preflight", return_value=pre), patch(
        "watcher.load_oci_config", return_value={"tenancy": "ocid1.tenancy.oc1..ten"}
    ), patch("watcher.OciFacade", return_value=facade), patch(
        "watcher.disable_timer", return_value=True
    ):
        outcome = watcher.run_once(cfg, dry_run=False)

    assert outcome["status"] == "WATCHING_CAPACITY"
    assert outcome["retryable"] is True


def test_auth_error_stops_timer(tmp_path):
    cfg = _base_config(tmp_path)
    pre = SimpleNamespace(ok=True, status="READY", messages=["ok"], details=_ok_preflight_details())
    facade = MagicMock()
    facade.launch_instance.side_effect = ServiceError(
        status=401,
        code="NotAuthenticated",
        headers={},
        message="Invalid key",
    )

    with patch("watcher.run_preflight", return_value=pre), patch(
        "watcher.load_oci_config", return_value={"tenancy": "ocid1.tenancy.oc1..ten"}
    ), patch("watcher.OciFacade", return_value=facade), patch(
        "watcher.disable_timer", return_value=True
    ) as disable:
        outcome = watcher.run_once(cfg, dry_run=False)

    assert outcome["status"] == "BLOCKED_AUTH"
    disable.assert_called()


def test_timeout_unclear_does_not_relaunch(tmp_path):
    cfg = _base_config(tmp_path)
    pre = SimpleNamespace(ok=True, status="READY", messages=["ok"], details=_ok_preflight_details())
    facade = MagicMock()
    facade.launch_instance.side_effect = TimeoutError("connection timed out")

    with patch("watcher.run_preflight", return_value=pre), patch(
        "watcher.load_oci_config", return_value={"tenancy": "ocid1.tenancy.oc1..ten"}
    ), patch("watcher.OciFacade", return_value=facade), patch(
        "watcher.disable_timer", return_value=True
    ) as disable:
        outcome = watcher.run_once(cfg, dry_run=False)

    assert outcome["status"] == "PENDING_CONFIRMATION"
    disable.assert_called()
