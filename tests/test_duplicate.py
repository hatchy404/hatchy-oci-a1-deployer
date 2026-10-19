from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import watcher
from state_manager import StateManager


def test_process_lock_blocks_concurrent(tmp_path):
    lock = tmp_path / "lock"
    with watcher.ProcessLock(lock):
        try:
            with watcher.ProcessLock(lock):
                assert False, "second lock should fail"
        except RuntimeError as exc:
            assert "Another watcher" in str(exc)


def test_completed_state_refuses_launch(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    sm = StateManager(state_dir, data_dir)
    sm.mark_success(
        {
            "instance_ocid": "ocid1.instance.oc1..done",
            "public_ip": "1.2.3.4",
            "checks": {"lifecycle_running": True},
        }
    )
    cfg = {
        "paths": {
            "state_dir": str(state_dir),
            "data_dir": str(data_dir),
            "lock_file": str(tmp_path / "lock"),
        },
        "notify": {"enabled": False},
    }
    with patch("watcher.run_preflight") as pre:
        outcome = watcher.run_once(cfg, dry_run=False)
        pre.assert_not_called()
    assert outcome["status"] == "SUCCESS"
    assert outcome["message"] == "Already completed"


def test_existing_ocid_confirms_instead_of_create(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    sm = StateManager(state_dir, data_dir)
    st = sm.load()
    st["instance_ocid"] = "ocid1.instance.oc1..pending"
    st["completed"] = False
    st["status"] = "PENDING_CONFIRMATION"
    sm.save(st)

    inst = SimpleNamespace(
        id="ocid1.instance.oc1..pending",
        display_name="HATCHY",
        lifecycle_state="PROVISIONING",
        shape="VM.Standard.A1.Flex",
        shape_config=SimpleNamespace(ocpus=2, memory_in_gbs=12),
        availability_domain="AD-1",
    )
    facade = MagicMock()
    facade.get_instance.return_value = inst

    cfg = {
        "region": "ap-tokyo-1",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_in_gbs": 12,
        "boot_volume_size_in_gbs": 100,
        "oci_config_path": "~/.oci/config",
        "oci_profile": "DEFAULT",
        "paths": {
            "state_dir": str(state_dir),
            "data_dir": str(data_dir),
            "lock_file": str(tmp_path / "lock"),
        },
        "notify": {"enabled": False},
    }

    with patch("watcher.load_oci_config", return_value={"tenancy": "ten"}), patch(
        "watcher.OciFacade", return_value=facade
    ), patch("watcher.disable_timer", return_value=True), patch(
        "watcher.run_preflight"
    ) as pre:
        outcome = watcher.run_once(cfg, dry_run=False)
        pre.assert_not_called()
        facade.launch_instance.assert_not_called()

    assert outcome["status"] == "PENDING_CONFIRMATION"
