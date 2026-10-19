from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import preflight
from oci_client import classify_service_error, find_ubuntu_2404_minimal_aarch64
from oci.exceptions import ServiceError


def test_missing_ssh_key(tmp_path):
    cfg = {
        "ssh_public_key_path": str(tmp_path / "missing.pub"),
        "oci_config_path": str(tmp_path / "no-config"),
        "region": "ap-tokyo-1",
    }
    res = preflight.run_preflight(cfg, require_live=False)
    assert res.ok is False
    assert res.status == "BLOCKED_CONFIGURATION"


def test_missing_auth(tmp_path):
    pub = tmp_path / "id.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForUnitTest user@host\n")
    cfg = {
        "ssh_public_key_path": str(pub),
        "oci_config_path": str(tmp_path / "missing-oci-config"),
        "oci_profile": "DEFAULT",
        "region": "ap-tokyo-1",
    }
    res = preflight.run_preflight(cfg, require_live=True)
    assert res.ok is False
    assert res.status == "BLOCKED_AUTH"


def test_home_region_mismatch():
    pub_path = "/tmp/hatchy-test.pub"
    # use mock for auth + facade
    facade = MagicMock()
    facade.get_tenancy.return_value = SimpleNamespace(name="hatchy404", home_region_key="NRT")
    facade.list_region_subscriptions.return_value = [
        SimpleNamespace(is_home_region=True, region_name="us-ashburn-1")
    ]

    with patch("preflight.read_ssh_public_key", return_value="ssh-ed25519 AAAA test"), patch(
        "preflight.get_auth_info",
        return_value=SimpleNamespace(
            tenancy_ocid="ocid1.tenancy.oc1..ten",
            user_ocid="ocid1.user.oc1..user",
            fingerprint="aa:bb",
            region="ap-tokyo-1",
            key_file="/tmp/key.pem",
            config_path="/tmp/config",
            profile="DEFAULT",
        ),
    ), patch("preflight.load_oci_config", return_value={"tenancy": "ocid1.tenancy.oc1..ten"}):
        res = preflight.run_preflight(
            {
                "region": "ap-tokyo-1",
                "ssh_public_key_path": pub_path,
                "vcn_ocid": "vcn",
                "subnet_ocid": "subnet",
            },
            facade=facade,
            require_live=True,
        )
    assert res.ok is False
    assert res.status == "BLOCKED_FREE_TIER"


def test_find_ubuntu_image_prefers_minimal_aarch64():
    images = [
        SimpleNamespace(
            display_name="Canonical-Ubuntu-24.04-2024.01-0",
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            lifecycle_state="AVAILABLE",
            id="img-x86",
            time_created=1,
            create_image_allowed=True,
        ),
        SimpleNamespace(
            display_name="Canonical-Ubuntu-24.04-Minimal-aarch64-2024.10-0",
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            lifecycle_state="AVAILABLE",
            id="img-arm-min",
            time_created=2,
            create_image_allowed=True,
        ),
        SimpleNamespace(
            display_name="Canonical-Ubuntu-24.04-aarch64-2024.10-0",
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            lifecycle_state="AVAILABLE",
            id="img-arm",
            time_created=3,
            create_image_allowed=True,
        ),
    ]
    chosen = find_ubuntu_2404_minimal_aarch64(images)
    assert chosen.id == "img-arm-min"


def test_classify_capacity_error():
    exc = ServiceError(
        status=500,
        code="InternalError",
        headers={},
        message="Out of host capacity",
    )
    info = classify_service_error(exc)
    assert info["category"] == "capacity"
    assert info["retryable"] is True


def test_classify_rate_limit():
    exc = ServiceError(status=429, code="TooManyRequests", headers={}, message="Slow down")
    info = classify_service_error(exc)
    assert info["category"] == "rate_limit"
    assert info["retryable"] is True


def test_a1_usage_fail_closed_on_existing():
    instances = [
        SimpleNamespace(
            shape="VM.Standard.A1.Flex",
            lifecycle_state="RUNNING",
            shape_config=SimpleNamespace(ocpus=2, memory_in_gbs=12),
            display_name="other",
            freeform_tags={},
        )
    ]
    used_o, used_m = preflight._sum_a1_usage(instances)
    assert used_o == 2
    assert used_m == 12
