"""URI / subscription import engine — mirrors src/http/import_engine.cpp."""
from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import unquote, parse_qs, urlparse

import httpx
import yaml

from models import ProxyNode, generate_id


# ── Utility ────────────────────────────────────────────────────────────────

def _b64decode(s: str) -> str:
    """Lenient base64 decode — adds padding if needed."""
    s = s.strip()
    pad = (-len(s)) % 4
    try:
        return base64.b64decode(s + "=" * pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _parse_host_port(s: str) -> tuple[str, int] | None:
    """Parse host:port or [ipv6]:port."""
    if s.startswith("["):
        m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", s)
        if m:
            host, port_s = m.group(1), m.group(2)
            return (host, int(port_s)) if port_s else None
        return None
    idx = s.rfind(":")
    if idx < 0:
        return None
    try:
        return s[:idx], int(s[idx + 1:])
    except ValueError:
        return None


def _parse_query(query: str) -> dict[str, str]:
    """Parse URL query string into a flat dict (first value of each key)."""
    result = {}
    for key, values in parse_qs(query, keep_blank_values=True).items():
        result[key] = unquote(values[0]) if values else ""
    return result


def _is_valid(n: ProxyNode) -> bool:
    return bool(n.type and n.server and n.port > 0)


# ── Protocol parsers ───────────────────────────────────────────────────────

def _parse_ss(uri: str) -> ProxyNode | None:
    """Parse ss:// URI."""
    data = uri[5:]  # strip "ss://"
    node = ProxyNode(id=generate_id(), type="ss", enabled=True)

    name = ""
    if "#" in data:
        data, frag = data.split("#", 1)
        name = unquote(frag)

    if "@" in data:
        userinfo_b64, hostport = data.rsplit("@", 1)
        userinfo = _b64decode(userinfo_b64)
        if ":" not in userinfo:
            return None
        method, password = userinfo.split(":", 1)
        hp = _parse_host_port(hostport)
        if hp is None:
            return None
        node.server, node.port = hp
        node.extra["cipher"] = method
        node.extra["password"] = password
        node.extra["udp"] = True
    else:
        decoded = _b64decode(data)
        m = re.match(r"^([^:]+):([^@]+)@(.+)$", decoded)
        if not m:
            return None
        method, password, hostport = m.group(1), m.group(2), m.group(3)
        hp = _parse_host_port(hostport)
        if hp is None:
            return None
        node.server, node.port = hp
        node.extra["cipher"] = method
        node.extra["password"] = password
        node.extra["udp"] = True

    node.name = name if name else f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


def _parse_vmess(uri: str) -> ProxyNode | None:
    """Parse vmess:// URI (base64-encoded JSON)."""
    data = uri[8:]
    decoded = _b64decode(data)
    try:
        j: dict[str, Any] = json.loads(decoded)
    except Exception:
        return None

    node = ProxyNode(id=generate_id(), type="vmess", enabled=True)
    node.name = j.get("ps") or j.get("name") or ""
    node.server = j.get("add", "")
    port_raw = j.get("port", 0)
    try:
        node.port = int(port_raw)
    except (ValueError, TypeError):
        return None

    node.extra["uuid"] = j.get("id", "")
    node.extra["alterId"] = j.get("aid", 0)
    node.extra["cipher"] = j.get("scy", "auto")
    node.extra["udp"] = True

    net = j.get("net", "tcp")
    if net == "ws":
        node.extra["network"] = "ws"
        ws_opts: dict[str, Any] = {"path": j.get("path", "/")}
        host = j.get("host", "")
        if host:
            ws_opts["headers"] = {"Host": host}
        node.extra["ws-opts"] = ws_opts
    elif net == "grpc":
        node.extra["network"] = "grpc"
        node.extra["grpc-opts"] = {"grpc-service-name": j.get("path", "")}
    elif net != "tcp":
        node.extra["network"] = net

    tls = j.get("tls", "")
    if tls == "tls":
        node.extra["tls"] = True
        sni = j.get("sni") or j.get("host", "")
        if sni:
            node.extra["servername"] = sni

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


def _parse_vless(uri: str) -> ProxyNode | None:
    """Parse vless:// URI."""
    parsed = urlparse(uri)
    node = ProxyNode(id=generate_id(), type="vless", enabled=True)
    node.name = unquote(parsed.fragment) if parsed.fragment else ""

    uuid = parsed.username or ""
    hp = _parse_host_port(f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname or "")
    if hp is None:
        return None
    node.server, node.port = hp
    node.extra["uuid"] = uuid
    node.extra["udp"] = True

    pm = _parse_query(parsed.query)
    net = pm.get("type", "tcp")
    security = pm.get("security", "")

    if net == "ws":
        node.extra["network"] = "ws"
        ws_opts: dict[str, Any] = {"path": pm.get("path", "/")}
        if pm.get("host"):
            ws_opts["headers"] = {"Host": pm["host"]}
        node.extra["ws-opts"] = ws_opts
    elif net == "grpc":
        node.extra["network"] = "grpc"
        node.extra["grpc-opts"] = {"grpc-service-name": pm.get("serviceName", "")}
    elif net != "tcp":
        node.extra["network"] = net

    if security == "tls":
        node.extra["tls"] = True
        if pm.get("sni"):
            node.extra["servername"] = pm["sni"]
        if pm.get("fp"):
            node.extra["client-fingerprint"] = pm["fp"]
    elif security == "reality":
        node.extra["tls"] = True
        reality: dict[str, Any] = {}
        if pm.get("pbk"):
            reality["public-key"] = pm["pbk"]
        if pm.get("sid"):
            reality["short-id"] = pm["sid"]
        node.extra["reality-opts"] = reality
        if pm.get("sni"):
            node.extra["servername"] = pm["sni"]
        if pm.get("fp"):
            node.extra["client-fingerprint"] = pm["fp"]

    if pm.get("flow"):
        node.extra["flow"] = pm["flow"]

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


def _parse_trojan(uri: str) -> ProxyNode | None:
    """Parse trojan:// URI."""
    parsed = urlparse(uri)
    node = ProxyNode(id=generate_id(), type="trojan", enabled=True)
    node.name = unquote(parsed.fragment) if parsed.fragment else ""

    password = unquote(parsed.username or "")
    hp = _parse_host_port(f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname or "")
    if hp is None:
        return None
    node.server, node.port = hp
    node.extra["password"] = password
    node.extra["udp"] = True

    pm = _parse_query(parsed.query)
    net = pm.get("type", "")
    if net == "ws":
        node.extra["network"] = "ws"
        ws_opts: dict[str, Any] = {"path": pm.get("path", "/")}
        if pm.get("host"):
            ws_opts["headers"] = {"Host": pm["host"]}
        node.extra["ws-opts"] = ws_opts
    elif net == "grpc":
        node.extra["network"] = "grpc"
        node.extra["grpc-opts"] = {"grpc-service-name": pm.get("serviceName", "")}

    if pm.get("sni"):
        node.extra["sni"] = pm["sni"]
    if pm.get("fp"):
        node.extra["client-fingerprint"] = pm["fp"]

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


def _parse_hysteria2(uri: str) -> ProxyNode | None:
    """Parse hysteria2:// or hy2:// URI."""
    # Normalise scheme so urlparse works
    normalised = re.sub(r"^hy2://", "hysteria2://", uri, flags=re.IGNORECASE)
    parsed = urlparse(normalised)
    node = ProxyNode(id=generate_id(), type="hysteria2", enabled=True)
    node.name = unquote(parsed.fragment) if parsed.fragment else ""

    password = unquote(parsed.username or "")
    hp = _parse_host_port(f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname or "")
    if hp is None:
        return None
    node.server, node.port = hp
    node.extra["password"] = password

    pm = _parse_query(parsed.query)
    if pm.get("sni"):
        node.extra["sni"] = pm["sni"]
    if pm.get("insecure") == "1":
        node.extra["skip-cert-verify"] = True
    if pm.get("obfs"):
        node.extra["obfs"] = pm["obfs"]
        if pm.get("obfs-password"):
            node.extra["obfs-password"] = pm["obfs-password"]

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


# ── Yaml proxy helper ──────────────────────────────────────────────────────

def _yaml_node_to_proxy(m: dict[str, Any]) -> ProxyNode | None:
    node = ProxyNode(id=generate_id(), enabled=True)
    node.name = str(m.get("name", ""))
    node.type = str(m.get("type", ""))
    node.server = str(m.get("server", ""))
    try:
        node.port = int(m.get("port", 0))
    except (ValueError, TypeError):
        pass
    for key, val in m.items():
        if key in ("name", "type", "server", "port"):
            continue
        node.extra[key] = val
    return node if _is_valid(node) else None


# ── Public API ─────────────────────────────────────────────────────────────

_SCHEME_PARSERS = {
    "ss://": _parse_ss,
    "vmess://": _parse_vmess,
    "vless://": _parse_vless,
    "trojan://": _parse_trojan,
    "hysteria2://": _parse_hysteria2,
    "hy2://": _parse_hysteria2,
}


def parse_uri(uri: str) -> list[ProxyNode]:
    uri = uri.strip()
    for scheme, parser in _SCHEME_PARSERS.items():
        if uri.lower().startswith(scheme):
            node = parser(uri)
            return [node] if node else []
    return []


def parse_lines(text: str) -> list[ProxyNode]:
    result: list[ProxyNode] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result.extend(parse_uri(line))
    return result


def parse_base64_text(text: str) -> list[ProxyNode]:
    decoded = _b64decode(text.strip())
    return parse_lines(decoded)


def parse_subscription(url: str) -> list[ProxyNode]:
    """Fetch subscription URL and parse nodes."""
    # Validate URL scheme (security: no file://, etc.)
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return []
    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        body = resp.text
    except Exception:
        return []

    nodes = parse_base64_text(body)
    if nodes:
        return nodes
    return parse_lines(body)


def parse_yaml_proxies(text: str) -> list[ProxyNode]:
    """Parse a YAML proxies block (Clash format)."""
    if not text.strip():
        return []
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return []

    proxies_raw: list[dict] = []
    if isinstance(doc, dict) and isinstance(doc.get("proxies"), list):
        proxies_raw = doc["proxies"]
    elif isinstance(doc, list):
        proxies_raw = doc
    elif isinstance(doc, dict) and "type" in doc:
        proxies_raw = [doc]

    result: list[ProxyNode] = []
    for item in proxies_raw:
        if isinstance(item, dict):
            node = _yaml_node_to_proxy(item)
            if node:
                result.append(node)
    return result


def parse_clash_yaml(text: str) -> list[ProxyNode]:
    return parse_yaml_proxies(text)
