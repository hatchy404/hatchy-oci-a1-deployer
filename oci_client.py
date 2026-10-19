#!/usr/bin/env python3
"""OCI SDK wrappers for HATCHY Always Free ARM acquisition."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import oci
from oci.exceptions import ConfigFileNotFound, InvalidConfig, ServiceError

logger = logging.getLogger(__name__)

ACTIVE_LIFECYCLES = {
    "PROVISIONING",
    "RUNNING",
    "STARTING",
    "STOPPING",
    "STOPPED",
    "CREATING_IMAGE",
    "MOVING",
}

RETRYABLE_CODES = {
    "InternalError",
    "InternalServerError",
    "TooManyRequests",
    "LimitExceeded",
    "RelatedResourceNotAuthorizedOrNotFound",  # sometimes transient on capacity races
}

CAPACITY_MARKERS = (
    "outofhostcapacity",
    "out of host capacity",
    "out of capacity",
    "insufficient host capacity",
    "host capacity",
)


@dataclass
class AuthInfo:
    tenancy_ocid: str
    user_ocid: str
    fingerprint: str
    region: str
    key_file: str
    config_path: str
    profile: str


def expand(path: str | Path) -> Path:
    return Path(os.path.expanduser(str(path))).resolve()


def load_oci_config(config_path: str, profile: str = "DEFAULT") -> dict[str, Any]:
    pem = os.environ.get("OCI_PRIVATE_KEY_PEM", "").strip()
    if pem:
        cfg = {
            "user": os.environ["OCI_USER_OCID"].strip(),
            "tenancy": os.environ["OCI_TENANCY_OCID"].strip(),
            "fingerprint": os.environ["OCI_FINGERPRINT"].strip(),
            "key_content": pem,
            "region": os.environ.get("OCI_REGION", "ap-tokyo-1").strip(),
        }
        oci.config.validate_config(cfg)
        return cfg
    path = expand(config_path)
    if not path.exists():
        raise FileNotFoundError(f"OCI config not found: {path}")
    cfg = oci.config.from_file(str(path), profile_name=profile)
    oci.config.validate_config(cfg)
    key_file = expand(cfg["key_file"])
    if not key_file.exists():
        raise FileNotFoundError(f"OCI API key not found: {key_file}")
    mode = key_file.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(
            f"OCI API key permissions too open ({oct(mode)}). Run: chmod 600 {key_file}"
        )
    cfg["key_file"] = str(key_file)
    return cfg


def get_auth_info(config_path: str, profile: str = "DEFAULT") -> AuthInfo:
    cfg = load_oci_config(config_path, profile)
    return AuthInfo(
        tenancy_ocid=cfg["tenancy"],
        user_ocid=cfg["user"],
        fingerprint=cfg["fingerprint"],
        region=cfg.get("region", ""),
        key_file=cfg.get("key_file") or "env:OCI_PRIVATE_KEY_PEM",
        config_path=str(expand(config_path)) if not os.environ.get("OCI_PRIVATE_KEY_PEM") else "env",
        profile=profile,
    )


class OciFacade:
    """Thin facade around OCI clients to simplify testing/mocking."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.identity = oci.identity.IdentityClient(config)
        self.compute = oci.core.ComputeClient(config)
        self.network = oci.core.VirtualNetworkClient(config)
        self.blockstorage = oci.core.BlockstorageClient(config)
        try:
            self.limits = oci.limits.LimitsClient(config)
        except Exception:  # noqa: BLE001
            self.limits = None

    def list_all(self, list_fn: Callable[..., Any], **kwargs: Any) -> list[Any]:
        items: list[Any] = []
        response = list_fn(**kwargs)
        items.extend(response.data or [])
        while response.has_next_page:
            response = list_fn(page=response.next_page, **kwargs)
            items.extend(response.data or [])
        return items

    def get_tenancy(self, tenancy_id: str):
        return self.identity.get_tenancy(tenancy_id).data

    def list_region_subscriptions(self, tenancy_id: str):
        return self.identity.list_region_subscriptions(tenancy_id).data

    def list_availability_domains(self, compartment_id: str):
        return self.identity.list_availability_domains(compartment_id).data

    def get_vcn(self, vcn_id: str):
        return self.network.get_vcn(vcn_id).data

    def get_subnet(self, subnet_id: str):
        return self.network.get_subnet(subnet_id).data

    def list_internet_gateways(self, compartment_id: str, vcn_id: str):
        return self.list_all(
            self.network.list_internet_gateways,
            compartment_id=compartment_id,
            vcn_id=vcn_id,
        )

    def get_route_table(self, rt_id: str):
        return self.network.get_route_table(rt_id).data

    def list_network_security_groups(self, compartment_id: str, vcn_id: str):
        return self.list_all(
            self.network.list_network_security_groups,
            compartment_id=compartment_id,
            vcn_id=vcn_id,
        )

    def get_security_list(self, security_list_id: str):
        return self.network.get_security_list(security_list_id).data

    def list_images(self, compartment_id: str, **kwargs: Any):
        return self.list_all(self.compute.list_images, compartment_id=compartment_id, **kwargs)

    def list_shapes(self, compartment_id: str, **kwargs: Any):
        return self.list_all(self.compute.list_shapes, compartment_id=compartment_id, **kwargs)

    def list_instances(self, compartment_id: str):
        return self.list_all(self.compute.list_instances, compartment_id=compartment_id)

    def get_instance(self, instance_id: str):
        return self.compute.get_instance(instance_id).data

    def list_vnic_attachments(self, compartment_id: str, instance_id: str):
        return self.list_all(
            self.compute.list_vnic_attachments,
            compartment_id=compartment_id,
            instance_id=instance_id,
        )

    def get_vnic(self, vnic_id: str):
        return self.network.get_vnic(vnic_id).data

    def list_boot_volume_attachments(self, ad: str, compartment_id: str, instance_id: str):
        return self.list_all(
            self.compute.list_boot_volume_attachments,
            availability_domain=ad,
            compartment_id=compartment_id,
            instance_id=instance_id,
        )

    def get_boot_volume(self, boot_volume_id: str):
        return self.blockstorage.get_boot_volume(boot_volume_id).data

    def list_volumes(self, compartment_id: str):
        return self.list_all(self.blockstorage.list_volumes, compartment_id=compartment_id)

    def list_boot_volumes(self, ad: str, compartment_id: str):
        return self.list_all(
            self.blockstorage.list_boot_volumes,
            availability_domain=ad,
            compartment_id=compartment_id,
        )

    def launch_instance(self, details, opc_retry_token: str):
        return self.compute.launch_instance(
            launch_instance_details=details,
            opc_retry_token=opc_retry_token,
        )


def classify_service_error(exc: ServiceError) -> dict[str, Any]:
    code = getattr(exc, "code", None) or ""
    message = getattr(exc, "message", None) or str(exc)
    status = getattr(exc, "status", None)
    lowered = f"{code} {message}".lower()

    is_capacity = any(marker in lowered for marker in CAPACITY_MARKERS) or code in {
        "InternalError",
        "OutOfHostCapacity",
    } and "capacity" in lowered

    # Explicit capacity codes/messages
    if "outofhostcapacity" in lowered or "out of host capacity" in lowered:
        is_capacity = True

    is_auth = status in {401, 404} and code in {
        "NotAuthenticated",
        "NotAuthorizedOrNotFound",
        "Unauthorized",
    }
    is_auth = is_auth or code in {"NotAuthenticated", "InvalidAuthenticationInfo"}
    is_permission = status == 403 or code in {
        "NotAuthorizedOrNotFound",
        "Forbidden",
        "AuthorizationFailed",
    }
    is_rate = status == 429 or code in {"TooManyRequests", "RateLimitExceeded"}
    is_transient = status in {500, 502, 503, 504} or code in RETRYABLE_CODES

    retryable = bool(is_capacity or is_rate or (is_transient and not is_auth))
    category = "unknown"
    if is_capacity:
        category = "capacity"
    elif is_rate:
        category = "rate_limit"
    elif is_auth:
        category = "auth"
    elif is_permission:
        category = "permission"
    elif is_transient:
        category = "transient"
    elif status and 400 <= int(status) < 500:
        category = "client_error"

    return {
        "code": code,
        "message": message,
        "status": status,
        "retryable": retryable,
        "category": category,
    }


def find_ubuntu_2404_minimal_aarch64(images: Iterable[Any]) -> Optional[Any]:
    candidates = []
    for img in images:
        name = (getattr(img, "display_name", "") or "").lower()
        os_name = (getattr(img, "operating_system", "") or "").lower()
        version = (getattr(img, "operating_system_version", "") or "").lower()
        lifecycle = getattr(img, "lifecycle_state", "")
        if lifecycle and lifecycle != "AVAILABLE":
            continue
        if "ubuntu" not in os_name and "ubuntu" not in name:
            continue
        if "24.04" not in version and "24.04" not in name:
            continue
        # Prefer aarch64/ARM; reject obvious x86 names
        if any(x in name for x in ("x86_64", "amd64", "x86-64")):
            continue
        if "aarch64" not in name and "arm64" not in name and "a1" not in name:
            # Some OCI Ubuntu images encode arch only in name; require arch hint
            continue
        score = 0
        if "minimal" in name:
            score += 10
        if "aarch64" in name or "arm64" in name:
            score += 5
        if getattr(img, "create_image_allowed", True) is not False:
            score += 1
        candidates.append((score, getattr(img, "time_created", None), img))

    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1] or 0), reverse=True)
    return candidates[0][2]


def build_launch_details(
    *,
    availability_domain: str,
    compartment_id: str,
    display_name: str,
    shape: str,
    ocpus: float,
    memory_in_gbs: float,
    image_id: str,
    boot_volume_size_in_gbs: int,
    boot_volume_vpus_per_gb: int,
    subnet_id: str,
    ssh_public_key: str,
    freeform_tags: dict[str, str],
):
    return oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=compartment_id,
        display_name=display_name,
        shape=shape,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=memory_in_gbs,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image_id,
            boot_volume_size_in_gbs=boot_volume_size_in_gbs,
            boot_volume_vpus_per_gb=boot_volume_vpus_per_gb,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=True,
            display_name=f"{display_name}-vnic",
        ),
        metadata={"ssh_authorized_keys": ssh_public_key.strip()},
        freeform_tags=freeform_tags,
    )


def new_opc_retry_token() -> str:
    # OCI opc-retry-token max length 64
    return uuid.uuid4().hex


def is_config_error(exc: Exception) -> bool:
    return isinstance(exc, (FileNotFoundError, PermissionError, ConfigFileNotFound, InvalidConfig, KeyError, ValueError))
