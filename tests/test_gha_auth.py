import os
from unittest.mock import patch

import oci_client
import watcher


def test_env_key_content_skips_config_file(monkeypatch):
    monkeypatch.setenv("OCI_PRIVATE_KEY_PEM", "unit-test-key-material")
    monkeypatch.setenv("OCI_USER_OCID", "ocid1.user.oc1..user")
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..ten")
    monkeypatch.setenv("OCI_FINGERPRINT", "aa:bb")
    monkeypatch.setenv("OCI_REGION", "ap-tokyo-1")
    with patch("oci_client.oci.config.validate_config") as validate:
        cfg = oci_client.load_oci_config("/does/not/exist")
    validate.assert_called_once()
    assert cfg["key_content"] == "unit-test-key-material"
    assert "key_file" not in cfg
    assert cfg["region"] == "ap-tokyo-1"


def test_disable_timer_uses_gh_on_actions(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/hatchy-oci-a1-deployer")
    with patch("watcher.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stderr = ""
        assert watcher.disable_timer() is True
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["gh", "workflow", "disable"]
    assert "systemctl" not in cmd
