import os
import re
import json
import socket
import logging
from pathlib import Path
from ipaddress import ip_network, ip_address
from typing import List, Dict, Any, Optional
import requests
import routeros_api
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("mikrotik_stream")

# Environment paths: local .env takes priority, fallback to ~/.restream_env
BASE_DIR = Path(__file__).resolve().parent
ENV_LOCAL = BASE_DIR / ".env"
ENV_HOME = Path.home() / ".restream_env"
CONFIG_FILE = BASE_DIR / "targets.json"

if ENV_LOCAL.exists():
    load_dotenv(ENV_LOCAL)
elif ENV_HOME.exists():
    load_dotenv(ENV_HOME)
else:
    load_dotenv()


def get_config_env() -> Dict[str, str]:
    return {
        "MIKROTIK_IP": os.getenv("MIKROTIK_IP", ""),
        "MIKROTIK_USER": os.getenv("MIKROTIK_USER", "admin"),
        "MIKROTIK_PASSWORD": os.getenv("MIKROTIK_PASSWORD", ""),
        "MIKROTIK_PORT": os.getenv("MIKROTIK_PORT", "8728"),
        "DEFAULT_GATEWAY": os.getenv("DEFAULT_GATEWAY", ""),
        "WEB_PORT": os.getenv("PORT", "18542"),
        "WEB_USERNAME": os.getenv("WEB_USERNAME", ""),
        "WEB_PASSWORD": os.getenv("WEB_PASSWORD", "")
    }


def save_config_env(updates: Dict[str, str]):
    target_env = ENV_LOCAL if ENV_LOCAL.exists() else (ENV_HOME if ENV_HOME.exists() else ENV_LOCAL)
    current = get_config_env()
    current.update(updates)
    
    with open(target_env, "w", encoding="utf-8") as f:
        for k, v in current.items():
            f.write(f"{k}={v}\n")
    
    # Reload in current process
    for k, v in updates.items():
        os.environ[k] = v


def load_targets() -> List[Dict[str, Any]]:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading targets.json: {e}")
            
    # Default initial targets
    default_targets = [
        {
            "id": "restream",
            "name": "Restream.io Servers",
            "type": "api",
            "url": "https://website-backend.restream.io/v2/public/servers/ip-addresses",
            "enabled": True,
            "prefix": 24,
            "comment": "Stream: Restream"
        },
        {
            "id": "aparat",
            "name": "Aparat RTMP CDN",
            "type": "domain",
            "domain": "rtmp.cdn.asset.aparat.com",
            "enabled": True,
            "prefix": 24,
            "comment": "Stream: Aparat RTMP"
        }
    ]
    save_targets(default_targets)
    return default_targets


def save_targets(targets: List[Dict[str, Any]]):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(targets, f, indent=2, ensure_ascii=False)


class MikroTikManager:
    def __init__(self, host: str = None, username: str = None, password: str = None, port: int = 8728):
        cfg = get_config_env()
        self.host = host or cfg.get("MIKROTIK_IP", "")
        self.username = username or cfg.get("MIKROTIK_USER", "admin")
        self.password = password or cfg.get("MIKROTIK_PASSWORD", "")
        self.port = int(port or cfg.get("MIKROTIK_PORT", 8728))
        self.connection = None

    def connect(self):
        if not self.host or not self.username:
            raise ValueError("MikroTik Host or Username is not configured.")
        return routeros_api.RouterOsApiPool(
            self.host,
            username=self.username,
            password=self.password,
            port=self.port,
            plaintext_login=True
        )

    def test_connection(self) -> Dict[str, Any]:
        try:
            pool = self.connect()
            api = pool.get_api()
            identity_res = api.get_resource('/system/identity').get()
            router_name = identity_res[0].get('name', 'MikroTik') if identity_res else 'MikroTik'
            pool.disconnect()
            return {"status": "ok", "message": f"Connected to {router_name} ({self.host})", "identity": router_name}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_live_interfaces(self) -> List[Dict[str, Any]]:
        """
        LIVE query of all interfaces, their assigned IPs, and their auto-detected Gateway IPs from /ip/route.
        """
        pool = self.connect()
        try:
            api = pool.get_api()
            raw_interfaces = api.get_resource('/interface').get()
            
            # Query IP addresses to match with interfaces
            ip_map = {}
            try:
                raw_ips = api.get_resource('/ip/address').get()
                for item in raw_ips:
                    iface = item.get('interface')
                    addr = item.get('address')
                    if iface and addr:
                        if iface not in ip_map:
                            ip_map[iface] = []
                        ip_map[iface].append(addr)
            except Exception as e:
                logger.warning(f"Could not fetch IP addresses: {e}")

            # Automatically detect Gateway IP and ISP comments from /ip/route
            iface_gateways = {}
            try:
                routes = api.get_resource('/ip/route').get()
                for r in routes:
                    gw_stat = r.get('gateway-status', '')
                    comm = r.get('comment', '')
                    dst = r.get('dst-address', '')
                    
                    # Search pattern like '192.168.0.1 reachable via ether1'
                    match = re.search(r'([0-9\.]+)\s+reachable via\s+(\S+)', gw_stat)
                    if match:
                        gw_ip = match.group(1)
                        iface_name = match.group(2)
                        # Prefer default route (0.0.0.0/0) gateways
                        if iface_name not in iface_gateways or dst == '0.0.0.0/0':
                            iface_gateways[iface_name] = {'gateway_ip': gw_ip, 'comment': comm}
            except Exception as e:
                logger.warning(f"Could not detect route gateways: {e}")

            interfaces = []
            for item in raw_interfaces:
                name = item.get('name', '')
                iface_type = item.get('type', 'unknown')
                running = item.get('running') == 'true'
                disabled = item.get('disabled') == 'true'
                comment = item.get('comment', '')
                ips = ip_map.get(name, [])

                gw_data = iface_gateways.get(name, {})
                gw_ip = gw_data.get('gateway_ip', '')
                isp = gw_data.get('comment', '')

                # Build descriptive label
                details = []
                if isp:
                    details.append(isp.upper())
                if gw_ip:
                    details.append(f"Gateway: {gw_ip}")
                elif ips:
                    details.append(f"IP: {ips[0]}")
                
                status_text = "Running" if running else "Down"
                details_str = f" [{', '.join(details)}]" if details else ""
                label = f"{name}{details_str} ({status_text})"

                interfaces.append({
                    "id": item.get('id') or item.get('.id'),
                    "name": name,
                    "type": iface_type,
                    "running": running,
                    "disabled": disabled,
                    "comment": comment,
                    "ips": ips,
                    "gateway_ip": gw_ip,
                    "isp": isp,
                    "label": label
                })

            return interfaces
        finally:
            pool.disconnect()

    def get_active_stream_routes(self) -> List[Dict[str, Any]]:
        """
        Retrieves all currently active streaming routes on MikroTik (tagged with 'Stream:' or 'Restream').
        """
        pool = self.connect()
        try:
            api = pool.get_api()
            route_resource = api.get_resource('/ip/route')
            all_routes = route_resource.get()
            
            stream_routes = []
            for r in all_routes:
                comment = r.get('comment', '')
                if comment.startswith("Stream:") or comment.startswith("Restream"):
                    stream_routes.append({
                        "id": r.get('id') or r.get('.id'),
                        "dst_address": r.get('dst-address', ''),
                        "gateway": r.get('gateway', ''),
                        "comment": comment,
                        "active": r.get('active', 'true'),
                        "disabled": r.get('disabled', 'false')
                    })
            return stream_routes
        finally:
            pool.disconnect()

    def sync_routes(self, gateway: str, targets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Synchronizes routes for all enabled targets to the specified gateway or interface.
        """
        if not gateway:
            raise ValueError("Gateway or Interface must be specified.")

        cidr_targets = []
        logs = []

        # 1. Gather all CIDRs
        for target in targets:
            if not target.get("enabled", True):
                continue
            
            prefix = int(target.get("prefix", 24))
            comment = target.get("comment", f"Stream: {target.get('name', 'Custom')}")
            t_type = target.get("type")

            if t_type == "api":
                url = target.get("url")
                logs.append(f"Fetching IPs from API: {url}")
                ips = fetch_ips_from_api(url)
                cidrs = convert_ips_to_cidrs(ips, prefix)
                for c in cidrs:
                    cidr_targets.append({"cidr": c, "comment": comment})
                logs.append(f"Fetched {len(ips)} IPs -> {len(cidrs)} CIDR blocks (/ {prefix}) for {target.get('name')}")

            elif t_type == "domain":
                domain = target.get("domain")
                logs.append(f"Resolving domain: {domain}")
                ips = resolve_domain_to_ips(domain)
                if not ips:
                    logs.append(f"Warning: Could not resolve domain {domain}")
                else:
                    cidrs = convert_ips_to_cidrs(ips, prefix)
                    for c in cidrs:
                        cidr_targets.append({"cidr": c, "comment": comment})
                    logs.append(f"Resolved {domain} to {ips} -> {len(cidrs)} CIDR blocks (/{prefix})")

            elif t_type in ["ip", "custom"]:
                val = target.get("value") or target.get("domain") or target.get("ip")
                if val:
                    cidrs = parse_single_target(val, prefix)
                    for c in cidrs:
                        cidr_targets.append({"cidr": c, "comment": comment})
                    logs.append(f"Processed target {val} -> {len(cidrs)} CIDR blocks")

        if not cidr_targets:
            return {"status": "error", "message": "No valid CIDRs found to route.", "logs": logs}

        # 2. Connect to MikroTik and apply
        logs.append(f"Connecting to MikroTik {self.host}...")
        pool = self.connect()
        added_count = 0
        updated_count = 0
        unchanged_count = 0

        try:
            api = pool.get_api()
            route_resource = api.get_resource('/ip/route')
            existing_routes = route_resource.get()
            existing_dict = {r.get('dst-address'): r for r in existing_routes if 'dst-address' in r}

            for item in cidr_targets:
                cidr = item["cidr"]
                comment = item["comment"]

                if cidr in existing_dict:
                    route = existing_dict[cidr]
                    current_gw = route.get('gateway')
                    current_comm = route.get('comment')
                    
                    if current_gw != gateway or current_comm != comment:
                        route_id = route.get('id') or route.get('.id')
                        route_resource.call('set', {'.id': route_id, 'gateway': gateway, 'comment': comment})
                        updated_count += 1
                        logs.append(f"Updated {cidr} -> Gateway: {gateway}")
                    else:
                        unchanged_count += 1
                else:
                    route_resource.add(dst_address=cidr, gateway=gateway, comment=comment)
                    added_count += 1
                    logs.append(f"Added {cidr} -> Gateway: {gateway}")

            return {
                "status": "ok",
                "gateway": gateway,
                "added": added_count,
                "updated": updated_count,
                "unchanged": unchanged_count,
                "total": len(cidr_targets),
                "logs": logs
            }
        finally:
            pool.disconnect()

    def clear_stream_routes(self) -> Dict[str, Any]:
        """
        Removes all stream routes added by this script.
        """
        pool = self.connect()
        try:
            api = pool.get_api()
            route_resource = api.get_resource('/ip/route')
            existing_routes = route_resource.get()
            removed_count = 0

            for r in existing_routes:
                comm = r.get('comment', '')
                if comm.startswith("Stream:") or comm.startswith("Restream"):
                    route_id = r.get('id') or r.get('.id')
                    route_resource.call('remove', {'.id': route_id})
                    removed_count += 1

            return {"status": "ok", "removed": removed_count}
        finally:
            pool.disconnect()


# --- IP / DNS Helper Functions ---

def fetch_ips_from_api(url: str) -> List[str]:
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return [line.strip() for line in resp.text.splitlines() if line.strip() and not line.startswith("#")]
    except Exception as e:
        logger.error(f"Error fetching IPs from API {url}: {e}")
        return []


def resolve_domain_to_ips(domain: str) -> List[str]:
    """
    Performs DNS resolution on a domain and returns all IPv4 addresses found.
    """
    try:
        clean_domain = domain.split("/")[0].split(":")[0].strip()
        results = socket.getaddrinfo(clean_domain, None, socket.AF_INET)
        ips = list(set([res[4][0] for res in results]))
        return ips
    except Exception as e:
        logger.error(f"DNS Resolution failed for {domain}: {e}")
        return []


def convert_ips_to_cidrs(ip_list: List[str], prefix: int = 24) -> List[str]:
    cidrs = set()
    for ip_str in ip_list:
        try:
            # If already contains CIDR notation
            if "/" in ip_str:
                net = ip_network(ip_str, strict=False)
                cidrs.add(str(net))
            else:
                net = ip_network(f"{ip_str}/{prefix}", strict=False)
                cidrs.add(str(net))
        except ValueError:
            logger.warning(f"Invalid IP address skipped: {ip_str}")
    return sorted(list(cidrs))


def parse_single_target(target_str: str, default_prefix: int = 24) -> List[str]:
    target_str = target_str.strip()
    if not target_str:
        return []

    # Check if it's already an IP or CIDR
    try:
        if "/" in target_str:
            net = ip_network(target_str, strict=False)
            return [str(net)]
        else:
            ip_obj = ip_address(target_str)
            net = ip_network(f"{ip_obj}/{default_prefix}", strict=False)
            return [str(net)]
    except ValueError:
        # Not a raw IP -> Treat as domain
        ips = resolve_domain_to_ips(target_str)
        return convert_ips_to_cidrs(ips, default_prefix)
