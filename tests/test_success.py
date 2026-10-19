from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import watcher
from state_manager import StateManager


def test_success_flow_disables_timer(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    cfg = {
        "instance_name": "HATCHY",
        "region": "ap-tokyo-1",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_in_gbs": 12,
        "boot_volume_size_in_gbs": 100,
        "boot_volume_vpus_per_gb": 10,
        "subnet_ocid": "ocid1.subnet.oc1..sub",
        "freeform_tags": {"managed_by": "hatchy-oci-a1-watcher", "project": "HATCHY"},
        "oci_config_path": "~/.oci/config",
        "oci_profile": "DEFAULT",
        "paths": {
            "state_dir": str(state_dir),
            "data_dir": str(data_dir),
            "lock_file": str(tmp_path / "lock"),
        },
        "poll": {"max_attempts": 3, "interval_seconds": 0},
        "notify": {"enabled": False},
    }

    pre = SimpleNamespace(
        ok=True,
        status="READY",
        messages=["ok"],
        details={
            "compartment_id": "ocid1.tenancy.oc1..ten",
            "availability_domain": "NRTK:AP-TOKYO-1-AD-1",
            "image": {"id": "ocid1.image.oc1..img"},
            "ssh_public_key": "ssh-ed25519 AAAA test",
        },
    )

    running = SimpleNamespace(
        id="ocid1.instance.oc1..ok",
        display_name="HATCHY",
        lifecycle_state="RUNNING",
        shape="VM.Standard.A1.Flex",
        shape_config=SimpleNamespace(ocpus=2.0, memory_in_gbs=12.0),
        availability_domain="NRTK:AP-TOKYO-1-AD-1",
    )
    response = SimpleNamespace(data=running, headers={"opc-request-id": "req-1"})

    facade = MagicMock()
    facade.launch_instance.return_value = response
    facade.get_instance.return_value = running
    facade.list_vnic_attachments.return_value = [
        SimpleNamespace(lifecycle_state="ATTACHED", vnic_id="ocid1.vnic.oc1..v")
    ]
    facade.get_vnic.return_value = SimpleNamespace(public_ip="203.0.113.10")
    facade.list_boot_volume_attachments.return_value = [
        SimpleNamespace(boot_volume_id="ocid1.bootvolume.oc1..b")
    ]
    facade.get_boot_volume.return_value = SimpleNamespace(size_in_gbs=100)

    with patch("watcher.run_preflight", return_value=pre), patch(
        "watcher.load_oci_config", return_value={"tenancy": "ocid1.tenancy.oc1..ten"}
    ), patch("watcher.OciFacade", return_value=facade), patch(
        "watcher.disable_timer", return_value=True
    ) as disable, patch("watcher.build_launch_details", return_value=object()):
        outcome = watcher.run_once(cfg, dry_run=False)

    assert outcome["status"] == "SUCCESS"
    assert outcome["result"]["public_ip"] == "203.0.113.10"
    disable.assert_called()
    sm = StateManager(state_dir, data_dir)
    assert sm.is_completed() is True

    # Re-run must not launch again
    with patch("watcher.run_preflight") as pre2, patch("watcher.OciFacade") as fac2:
        again = watcher.run_once(cfg, dry_run=False)
        pre2.assert_not_called()
        fac2.assert_not_called()
    assert again["status"] == "SUCCESS"


def test_public_ip_missing_is_pending(tmp_path):
    state_dir = tmp_path / "state"
    data_dir = tmp_path / "data"
    cfg = {
        "instance_name": "HATCHY",
        "region": "ap-tokyo-1",
        "shape": "VM.Standard.A1.Flex",
        "ocpus": 2,
        "memory_in_gbs": 12,
        "boot_volume_size_in_gbs": 100,
        "boot_volume_vpus_per_gb": 10,
        "subnet_ocid": "ocid1.subnet.oc1..sub",
        "freeform_tags": {},
        "oci_config_path": "~/.oci/config",
        "oci_profile": "DEFAULT",
        "paths": {
            "state_dir": str(state_dir),
            "data_dir": str(data_dir),
            "lock_file": str(tmp_path / "lock"),
        },
        "poll": {"max_attempts": 2, "interval_seconds": 0},
        "notify": {"enabled": False},
    }
    pre = SimpleNamespace(
        ok=True,
        status="READY",
        messages=["ok"],
        details={
            "compartment_id": "ocid1.tenancy.oc1..ten",
            "availability_domain": "NRTK:AP-TOKYO-1-AD-1",
            "image": {"id": "ocid1.image.oc1..img"},
            "ssh_public_key": "ssh-ed25519 AAAA test",
        },
    )
    running = SimpleNamespace(
        id="ocid1.instance.oc1..ok",
        display_name="HATCHY",
        lifecycle_state="RUNNING",
        shape="VM.Standard.A1.Flex",
        shape_config=SimpleNamespace(ocpus=2.0, memory_in_gbs=12.0),
        availability_domain="NRTK:AP-TOKYO-1-AD-1",
    )
    facade = MagicMock()
    facade.launch_instance.return_value = SimpleNamespace(data=running, headers={})
    facade.get_instance.return_value = running
    facade.list_vnic_attachments.return_value = [
        SimpleNamespace(lifecycle_state="ATTACHED", vnic_id="ocid1.vnic.oc1..v")
    ]
    facade.get_vnic.return_value = SimpleNamespace(public_ip=None)
    facade.list_boot_volume_attachments.return_value = [
        SimpleNamespace(boot_volume_id="ocid1.bootvolume.oc1..b")
    ]
    facade.get_boot_volume.return_value = SimpleNamespace(size_in_gbs=100)

    with patch("watcher.run_preflight", return_value=pre), patch(
        "watcher.load_oci_config", return_value={"tenancy": "ten"}
    ), patch("watcher.OciFacade", return_value=facade), patch(
        "watcher.disable_timer", return_value=True
    ) as disable, patch("watcher.build_launch_details", return_value=object()):
        outcome = watcher.run_once(cfg, dry_run=False)

    assert outcome["status"] == "PENDING_CONFIRMATION"
    disable.assert_called()
