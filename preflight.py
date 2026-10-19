#!/usr/bin/env python3
"""Read-only preflight checks before any LaunchInstance call."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from oci.exceptions import ServiceError

from oci_client import (
    OciFacade,
    classify_service_error,
    find_ubuntu_2404_minimal_aarch64,
    get_auth_info,
    load_oci_config,
)

logger = logging.getLogger(__name__)


@dataclass
class PreflightResult:
    ok: bool
    status: str
    messages: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def fail(self, status: str, message: str) -> "PreflightResult":
        self.ok = False
        self.status = status
        self.messages.append(message)
        return self

    def note(self, message: str) -> None:
        self.messages.append(message)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def load_app_config(config_path: Optional[str] = None) -> dict[str, Any]:
    import yaml

    candidates = []
    if config_path:
        candidates.append(expand(config_path))
    candidates.append(expand("~/.config/oci-a1-watcher/config.yaml"))
    candidates.append(expand(str(Path(__file__).resolve().parent / "config.yaml")))

    example = expand(str(Path(__file__).resolve().parent / "config.example.yaml"))
    chosen = next((p for p in candidates if p.exists()), example)
    with chosen.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data["_config_path"] = str(chosen)
    overlays = {
        "region": "OCI_REGION",
        "vcn_ocid": "OCI_VCN_OCID",
        "subnet_ocid": "OCI_SUBNET_OCID",
        "ssh_public_key_path": "OCI_SSH_PUBLIC_KEY_PATH",
        "oci_config_path": "OCI_CONFIG_PATH",
    }
    for field, env_name in overlays.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            data[field] = value
    return data


def read_ssh_public_key(path: str) -> str:
    env_key = os.environ.get("OCI_SSH_PUBLIC_KEY", "").strip()
    if env_key:
        if not env_key.startswith(("ssh-", "ecdsa-", "sk-")):
            raise ValueError("OCI_SSH_PUBLIC_KEY is not a public key")
        return env_key
    pub = expand(path)
    if not pub.exists():
        raise FileNotFoundError(f"SSH public key not found: {pub}")
    text = pub.read_text(encoding="utf-8").strip()
    if not text or not text.startswith(("ssh-", "ecdsa-", "sk-")):
        raise ValueError(f"Invalid SSH public key content in {pub}")
    return text


def _sum_a1_usage(instances: list[Any]) -> tuple[float, float]:
    ocpus = 0.0
    mem = 0.0
    for inst in instances:
        shape = getattr(inst, "shape", "") or ""
        state = getattr(inst, "lifecycle_state", "") or ""
        if state == "TERMINATED":
            continue
        if "A1" not in shape.upper() and shape != "VM.Standard.A1.Flex":
            continue
        cfg = getattr(inst, "shape_config", None)
        if cfg is not None:
            ocpus += float(getattr(cfg, "ocpus", 0) or 0)
            mem += float(getattr(cfg, "memory_in_gbs", 0) or 0)
        else:
            # Fail closed if A1 present but shape config missing
            ocpus += 999
            mem += 999
    return ocpus, mem


def _sum_storage_gbs(boot_volumes: list[Any], volumes: list[Any]) -> float:
    total = 0.0
    for bv in boot_volumes:
        state = getattr(bv, "lifecycle_state", "") or ""
        if state in {"AVAILABLE", "PROVISIONING", "RESTORING"}:
            total += float(getattr(bv, "size_in_gbs", 0) or 0)
    for vol in volumes:
        state = getattr(vol, "lifecycle_state", "") or ""
        if state in {"AVAILABLE", "PROVISIONING", "RESTORING"}:
            total += float(getattr(vol, "size_in_gbs", 0) or 0)
    return total


def find_existing_hatchy(instances: list[Any], name: str, tags: dict[str, str]) -> Optional[Any]:
    for inst in instances:
        state = getattr(inst, "lifecycle_state", "") or ""
        if state == "TERMINATED":
            continue
        display = getattr(inst, "display_name", "") or ""
        freeform = getattr(inst, "freeform_tags", None) or {}
        if display == name:
            return inst
        if tags and all(freeform.get(k) == v for k, v in tags.items()):
            return inst
    return None


def check_ssh_ingress(security_lists: list[Any]) -> dict[str, Any]:
    """Inspect whether SSH appears allowed. Does not modify rules."""
    ssh_rules = []
    open_world = False
    for sl in security_lists:
        for rule in getattr(sl, "ingress_security_rules", None) or []:
            proto = str(getattr(rule, "protocol", ""))
            source = getattr(rule, "source", "")
            tcp = getattr(rule, "tcp_options", None)
            if proto not in {"6", "all"} and proto != "6":
                # protocol 6 = TCP; "all" may also allow SSH
                if proto != "all":
                    continue
            port_ok = False
            if proto == "all" or tcp is None:
                port_ok = True
            else:
                dest = getattr(tcp, "destination_port_range", None)
                if dest is None:
                    port_ok = True
                else:
                    mn = getattr(dest, "min", None)
                    mx = getattr(dest, "max", None)
                    if mn is not None and mx is not None and mn <= 22 <= mx:
                        port_ok = True
            if port_ok:
                ssh_rules.append({"security_list_id": sl.id, "source": source, "protocol": proto})
                if source in {"0.0.0.0/0", "::/0"}:
                    open_world = True
    return {"ssh_rules": ssh_rules, "open_to_world": open_world, "has_ssh_rule": bool(ssh_rules)}


def run_preflight(
    app_config: dict[str, Any],
    facade: Optional[OciFacade] = None,
    *,
    require_live: bool = True,
) -> PreflightResult:
    result = PreflightResult(ok=True, status="READY")
    region = app_config.get("region", "ap-tokyo-1")
    oci_config_path = app_config.get("oci_config_path", "~/.oci/config")
    profile = app_config.get("oci_profile", "DEFAULT")

    # SSH key check (local)
    try:
        ssh_key = read_ssh_public_key(app_config.get("ssh_public_key_path", "~/.ssh/id_ed25519.pub"))
        result.details["ssh_public_key_path"] = str(
            expand(app_config.get("ssh_public_key_path", "~/.ssh/id_ed25519.pub"))
        )
        result.details["ssh_public_key_fingerprint_hint"] = ssh_key.split()[0:2]
        result.details["ssh_public_key"] = ssh_key
    except Exception as exc:  # noqa: BLE001
        return result.fail("BLOCKED_CONFIGURATION", f"SSH public key unavailable: {exc}")

    # Auth check
    try:
        auth = get_auth_info(oci_config_path, profile)
        cfg = load_oci_config(oci_config_path, profile)
    except Exception as exc:  # noqa: BLE001
        return result.fail(
            "BLOCKED_AUTH",
            f"OCI API authentication is not ready: {exc}",
        )

    if auth.region and auth.region != region:
        return result.fail(
            "BLOCKED_CONFIGURATION",
            f"OCI config region is '{auth.region}', expected '{region}'",
        )

    result.details["auth"] = {
        "tenancy_ocid": auth.tenancy_ocid,
        "user_ocid": auth.user_ocid,
        "fingerprint": auth.fingerprint,
        "region": auth.region or region,
        "config_path": auth.config_path,
        "profile": auth.profile,
        # Never include key material
    }

    if not require_live:
        result.note("Live OCI checks skipped")
        return result

    if facade is None:
        facade = OciFacade(cfg)

    compartment_id = auth.tenancy_ocid  # root compartment
    result.details["compartment_id"] = compartment_id

    try:
        tenancy = facade.get_tenancy(auth.tenancy_ocid)
        result.details["tenancy_name"] = getattr(tenancy, "name", None)
        home_region_key = getattr(tenancy, "home_region_key", None)
        result.details["home_region_key"] = home_region_key

        subscriptions = facade.list_region_subscriptions(auth.tenancy_ocid)
        home_region = None
        for sub in subscriptions:
            if getattr(sub, "is_home_region", False):
                home_region = getattr(sub, "region_name", None)
                break
        result.details["home_region"] = home_region
        if home_region != region:
            return result.fail(
                "BLOCKED_FREE_TIER",
                f"Home Region is '{home_region}', Always Free Compute must be created in Home Region '{region}'",
            )

        ads = facade.list_availability_domains(compartment_id)
        if not ads:
            return result.fail("BLOCKED_CONFIGURATION", "No availability domains found")
        ad_index = int(app_config.get("availability_domain_index", 1)) - 1
        if ad_index < 0 or ad_index >= len(ads):
            return result.fail(
                "BLOCKED_CONFIGURATION",
                f"AD index {ad_index + 1} out of range; found {len(ads)} ADs",
            )
        # Prefer name containing AD-1
        chosen_ad = None
        for ad in ads:
            if "AD-1" in (ad.name or ""):
                chosen_ad = ad
                break
        if chosen_ad is None:
            chosen_ad = ads[ad_index]
        result.details["availability_domain"] = chosen_ad.name
        result.details["availability_domains"] = [ad.name for ad in ads]

        # VCN / Subnet
        vcn_ocid = app_config["vcn_ocid"]
        subnet_ocid = app_config["subnet_ocid"]
        vcn = facade.get_vcn(vcn_ocid)
        subnet = facade.get_subnet(subnet_ocid)
        result.details["vcn"] = {
            "id": vcn.id,
            "name": vcn.display_name,
            "state": vcn.lifecycle_state,
        }
        result.details["subnet"] = {
            "id": subnet.id,
            "name": subnet.display_name,
            "state": subnet.lifecycle_state,
            "vcn_id": subnet.vcn_id,
            "prohibit_public_ip": getattr(subnet, "prohibit_public_ip_on_vnic", None),
        }
        if subnet.vcn_id != vcn.id:
            return result.fail("BLOCKED_NETWORK", "Subnet does not belong to the specified VCN")
        if subnet.lifecycle_state != "AVAILABLE" or vcn.lifecycle_state != "AVAILABLE":
            return result.fail("BLOCKED_NETWORK", "VCN or Subnet is not AVAILABLE")
        if getattr(subnet, "prohibit_public_ip_on_vnic", False):
            return result.fail("BLOCKED_NETWORK", "Subnet prohibits public IPv4 on VNIC")

        igws = facade.list_internet_gateways(compartment_id, vcn.id)
        active_igw = [g for g in igws if getattr(g, "lifecycle_state", "") == "AVAILABLE" and getattr(g, "is_enabled", True)]
        result.details["internet_gateways"] = [
            {"id": g.id, "name": g.display_name, "enabled": getattr(g, "is_enabled", None)} for g in igws
        ]
        if not active_igw:
            result.note("WARNING: No enabled Internet Gateway found on VCN")
            result.details["network_warning"] = "missing_internet_gateway"

        rt = facade.get_route_table(subnet.route_table_id)
        routes = []
        has_igw_route = False
        for rule in getattr(rt, "route_rules", None) or []:
            target = getattr(rule, "network_entity_id", "") or ""
            cidr = getattr(rule, "destination", "") or ""
            routes.append({"destination": cidr, "network_entity_id": target})
            if cidr in {"0.0.0.0/0", "::/0"} and any(g.id == target for g in active_igw):
                has_igw_route = True
        result.details["route_table"] = {"id": rt.id, "routes": routes, "has_igw_default_route": has_igw_route}
        if not has_igw_route:
            result.note("WARNING: Default route to Internet Gateway not found on subnet route table")
            result.details["network_warning"] = "missing_igw_route"

        security_lists = []
        for sl_id in getattr(subnet, "security_list_ids", None) or []:
            security_lists.append(facade.get_security_list(sl_id))
        ssh_info = check_ssh_ingress(security_lists)
        result.details["ssh_ingress"] = ssh_info
        if not ssh_info["has_ssh_rule"]:
            result.note("WARNING: No explicit TCP/22 ingress rule found on subnet security lists")

        # Shape check
        shapes = facade.list_shapes(compartment_id, availability_domain=chosen_ad.name)
        shape_names = {s.shape for s in shapes}
        target_shape = app_config.get("shape", "VM.Standard.A1.Flex")
        if target_shape not in shape_names:
            return result.fail(
                "BLOCKED_CONFIGURATION",
                f"Shape {target_shape} not available in {chosen_ad.name}",
            )
        result.details["shape_available"] = True

        # Image selection
        images = facade.list_images(
            compartment_id,
            operating_system="Canonical Ubuntu",
            operating_system_version="24.04",
            shape=target_shape,
            lifecycle_state="AVAILABLE",
        )
        # Also list broader and filter locally if needed
        if not images:
            images = facade.list_images(compartment_id, shape=target_shape, lifecycle_state="AVAILABLE")
        image = find_ubuntu_2404_minimal_aarch64(images)
        if image is None:
            # Second pass without OS filters
            all_images = facade.list_images(compartment_id, lifecycle_state="AVAILABLE")
            image = find_ubuntu_2404_minimal_aarch64(all_images)
        if image is None:
            return result.fail(
                "BLOCKED_CONFIGURATION",
                "No Canonical Ubuntu 24.04 Minimal aarch64 image found for A1.Flex",
            )
        result.details["image"] = {
            "id": image.id,
            "display_name": image.display_name,
            "operating_system": image.operating_system,
            "operating_system_version": image.operating_system_version,
        }

        # Existing instances / free tier
        instances = facade.list_instances(compartment_id)
        hatchy = find_existing_hatchy(
            instances,
            app_config.get("instance_name", "HATCHY"),
            app_config.get("freeform_tags") or {},
        )
        if hatchy is not None:
            result.details["existing_hatchy"] = {
                "id": hatchy.id,
                "display_name": hatchy.display_name,
                "lifecycle_state": hatchy.lifecycle_state,
                "shape": hatchy.shape,
            }
            return result.fail(
                "SUCCESS" if hatchy.lifecycle_state == "RUNNING" else "BLOCKED_CONFIGURATION",
                f"Existing HATCHY instance found ({hatchy.lifecycle_state}): {hatchy.id}",
            )

        used_ocpu, used_mem = _sum_a1_usage(instances)
        need_ocpu = float(app_config.get("ocpus", 2))
        need_mem = float(app_config.get("memory_in_gbs", 12))
        max_ocpu = float(app_config.get("free_tier", {}).get("max_a1_ocpus", 2))
        max_mem = float(app_config.get("free_tier", {}).get("max_a1_memory_gbs", 12))
        result.details["a1_usage"] = {
            "used_ocpus": used_ocpu,
            "used_memory_gbs": used_mem,
            "need_ocpus": need_ocpu,
            "need_memory_gbs": need_mem,
            "max_ocpus": max_ocpu,
            "max_memory_gbs": max_mem,
        }
        if used_ocpu + need_ocpu > max_ocpu + 1e-9 or used_mem + need_mem > max_mem + 1e-9:
            return result.fail(
                "BLOCKED_FREE_TIER",
                f"A1 free-tier capacity insufficient: used {used_ocpu} OCPU/{used_mem}GB, "
                f"need +{need_ocpu}/{need_mem}, max {max_ocpu}/{max_mem}",
            )

        # Storage usage across ADs
        storage_total = 0.0
        for ad in ads:
            bvs = facade.list_boot_volumes(ad.name, compartment_id)
            storage_total += _sum_storage_gbs(bvs, [])
        volumes = facade.list_volumes(compartment_id)
        storage_total += _sum_storage_gbs([], volumes)
        need_boot = float(app_config.get("boot_volume_size_in_gbs", 100))
        max_storage = float(app_config.get("free_tier", {}).get("max_storage_gbs", 200))
        result.details["storage_usage"] = {
            "used_gbs": storage_total,
            "need_boot_gbs": need_boot,
            "max_gbs": max_storage,
        }
        if storage_total + need_boot > max_storage + 1e-9:
            return result.fail(
                "BLOCKED_FREE_TIER",
                f"Free storage limit would be exceeded: used {storage_total}GB + boot {need_boot}GB > {max_storage}GB",
            )

        result.note("Preflight checks passed")
        return result

    except ServiceError as exc:
        info = classify_service_error(exc)
        result.details["service_error"] = info
        if info["category"] in {"auth", "permission"}:
            return result.fail("BLOCKED_AUTH", f"OCI API denied: {info['code']}: {info['message']}")
        return result.fail("ERROR", f"OCI preflight failed: {info['code']}: {info['message']}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected preflight failure")
        return result.fail("ERROR", f"Unexpected preflight failure: {exc}")


def main() -> int:
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_app_config(sys.argv[1] if len(sys.argv) > 1 else None)
    res = run_preflight(cfg)
    # Avoid dumping SSH key material in CLI output
    details = dict(res.details)
    details.pop("ssh_public_key", None)
    print(
        json.dumps(
            {"ok": res.ok, "status": res.status, "messages": res.messages, "details": details},
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
    return 0 if res.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
