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


def _truthy(value: str) -> bool:
    return value.lower() in ("1", "true", "yes", "on")


def _parsed_host_port(parsed: Any) -> tuple[str, int] | None:
    """Extract host/port from urlparse result, handling invalid ports."""
    try:
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        return None
    if not host or port is None:
        return None
    return host, int(port)


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


def _parse_http(uri: str) -> ProxyNode | None:
    """Parse http:// or https:// proxy URI."""
    parsed = urlparse(uri)
    node = ProxyNode(id=generate_id(), type="http", enabled=True)
    node.name = unquote(parsed.fragment) if parsed.fragment else ""

    hp = _parsed_host_port(parsed)
    if hp is None:
        return None
    node.server, node.port = hp

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if username:
        node.extra["username"] = username
    if password:
        node.extra["password"] = password

    pm = _parse_query(parsed.query)
    if pm.get("username") and "username" not in node.extra:
        node.extra["username"] = pm["username"]
    if pm.get("password") and "password" not in node.extra:
        node.extra["password"] = pm["password"]

    if parsed.scheme.lower() == "https" or _truthy(pm.get("tls", "")) or pm.get("security") == "tls":
        node.extra["tls"] = True
    if pm.get("sni"):
        node.extra["sni"] = pm["sni"]
    if _truthy(pm.get("skip-cert-verify", "")) or _truthy(pm.get("allowInsecure", "")):
        node.extra["skip-cert-verify"] = True

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


def _parse_socks5(uri: str) -> ProxyNode | None:
    """Parse socks5://, socks5h://, or socks:// proxy URI."""
    parsed = urlparse(uri)
    node = ProxyNode(id=generate_id(), type="socks5", enabled=True)
    node.name = unquote(parsed.fragment) if parsed.fragment else ""

    hp = _parsed_host_port(parsed)
    if hp is None:
        return None
    node.server, node.port = hp

    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if username:
        node.extra["username"] = username
    if password:
        node.extra["password"] = password

    pm = _parse_query(parsed.query)
    if pm.get("username") and "username" not in node.extra:
        node.extra["username"] = pm["username"]
    if pm.get("password") and "password" not in node.extra:
        node.extra["password"] = pm["password"]
    if _truthy(pm.get("udp", "")):
        node.extra["udp"] = True
    if parsed.scheme.lower() == "socks5h" or _truthy(pm.get("remote-dns", "")):
        node.extra["remote-dns"] = True

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


# ── sing-box outbound helper ───────────────────────────────────────────────

_SING_BOX_TYPE_MAP = {
    "shadowsocks": "ss",
    "socks": "socks5",
    "http": "http",
    "vmess": "vmess",
    "vless": "vless",
    "trojan": "trojan",
    "hysteria2": "hysteria2",
    "tuic": "tuic",
}


def _copy_if_present(src: dict[str, Any], dst: dict[str, Any], src_key: str, dst_key: str | None = None) -> None:
    if src_key in src and src[src_key] not in (None, ""):
        dst[dst_key or src_key] = src[src_key]


def _mbps_to_mihomo(value: Any) -> str | None:
    """Convert sing-box's numeric Mbps field to Mihomo's bandwidth syntax."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return None
    if speed <= 0:
        return None
    rendered = str(int(speed)) if speed.is_integer() else str(speed)
    return f"{rendered} Mbps"


def _merge_singbox_tls(node_type: str, tls: Any, extra: dict[str, Any]) -> None:
    if not isinstance(tls, dict):
        return
    if tls.get("enabled") is False:
        return
    if node_type in ("vmess", "vless"):
        extra["tls"] = True
    _copy_if_present(tls, extra, "server_name", "sni")
    _copy_if_present(tls, extra, "alpn")
    if "insecure" in tls:
        extra["skip-cert-verify"] = bool(tls["insecure"])

    # sing-box pins the certificate public key, while Mihomo's `fingerprint`
    # field expects a SHA-256 hash of the complete certificate.  Copying the
    # base64 public-key pin into `fingerprint` creates an invalid config.  For
    # Hysteria2, use the only interoperable fallback: skip CA verification.
    # The exact raw outbound remains stored below, so sing-box keeps its secure
    # public-key pin when it is used for a port binding.
    if node_type == "hysteria2" and tls.get("certificate_public_key_sha256"):
        extra["skip-cert-verify"] = True

    utls = tls.get("utls")
    if isinstance(utls, dict):
        _copy_if_present(utls, extra, "fingerprint", "client-fingerprint")

    reality = tls.get("reality")
    if isinstance(reality, dict):
        reality_opts: dict[str, Any] = {}
        _copy_if_present(reality, reality_opts, "public_key", "public-key")
        _copy_if_present(reality, reality_opts, "short_id", "short-id")
        if reality_opts:
            extra["reality-opts"] = reality_opts


def _merge_singbox_transport(transport: Any, extra: dict[str, Any]) -> None:
    if not isinstance(transport, dict):
        return
    transport_type = str(transport.get("type", ""))
    if not transport_type:
        return
    extra["network"] = transport_type
    if transport_type == "ws":
        ws_opts: dict[str, Any] = {}
        _copy_if_present(transport, ws_opts, "path")
        headers = transport.get("headers")
        if isinstance(headers, dict) and headers:
            ws_opts["headers"] = headers
        if ws_opts:
            extra["ws-opts"] = ws_opts
    elif transport_type == "grpc":
        grpc_opts: dict[str, Any] = {}
        _copy_if_present(transport, grpc_opts, "service_name", "grpc-service-name")
        if grpc_opts:
            extra["grpc-opts"] = grpc_opts


def _singbox_outbound_to_proxy(m: dict[str, Any]) -> ProxyNode | None:
    sing_type = str(m.get("type", ""))
    node_type = _SING_BOX_TYPE_MAP.get(sing_type)
    if not node_type:
        return None

    node = ProxyNode(id=generate_id(), enabled=True)
    node.name = str(m.get("tag", ""))
    node.type = node_type
    node.server = str(m.get("server", ""))
    try:
        node.port = int(m.get("server_port", m.get("port", 0)))
    except (ValueError, TypeError):
        return None

    _copy_if_present(m, node.extra, "password")
    _copy_if_present(m, node.extra, "uuid")
    _copy_if_present(m, node.extra, "username")
    _copy_if_present(m, node.extra, "method", "cipher")
    _copy_if_present(m, node.extra, "alter_id", "alterId")
    _copy_if_present(m, node.extra, "security", "cipher")
    _copy_if_present(m, node.extra, "flow")

    if sing_type == "hysteria2":
        # sing-box uses numeric Mbps fields, while Mihomo expects values such
        # as "200 Mbps".  Omitting these makes many Hysteria2 servers fail
        # bandwidth negotiation and consequently delay tests.
        up = _mbps_to_mihomo(m.get("up_mbps"))
        down = _mbps_to_mihomo(m.get("down_mbps"))
        if up:
            node.extra["up"] = up
        if down:
            node.extra["down"] = down
        _copy_if_present(m, node.extra, "obfs")
        _copy_if_present(m, node.extra, "obfs_password", "obfs-password")

    _merge_singbox_transport(m.get("transport"), node.extra)
    _merge_singbox_tls(node_type, m.get("tls"), node.extra)

    # Preserve the exact sing-box outbound for future sing-box config generation.
    node.extra["source-format"] = "sing-box"
    node.extra["sing-box-outbound"] = m

    if not node.name:
        node.name = f"{node.server}:{node.port}"
    return node if _is_valid(node) else None


# ── Public API ─────────────────────────────────────────────────────────────

_SCHEME_PARSERS = {
    "http://": _parse_http,
    "https://": _parse_http,
    "socks5://": _parse_socks5,
    "socks5h://": _parse_socks5,
    "socks://": _parse_socks5,
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


def parse_singbox_json(text: str) -> list[ProxyNode]:
    """Parse sing-box outbound JSON into the common ProxyNode model."""
    if not text.strip():
        return []
    try:
        doc = json.loads(text)
    except Exception:
        return []

    outbounds_raw: list[Any] = []
    if isinstance(doc, dict) and isinstance(doc.get("outbounds"), list):
        outbounds_raw = doc["outbounds"]
    elif isinstance(doc, list):
        outbounds_raw = doc
    elif isinstance(doc, dict) and "type" in doc:
        outbounds_raw = [doc]

    result: list[ProxyNode] = []
    for item in outbounds_raw:
        if isinstance(item, dict):
            node = _singbox_outbound_to_proxy(item)
            if node:
                result.append(node)
    return result


def singbox_mihomo_compatibility_warnings(text: str) -> list[str]:
    """Return conversion caveats that should be shown after a sing-box import.

    The two cores implement Hysteria2 certificate pinning differently: sing-box
    pins a certificate *public key*, whereas Mihomo's `fingerprint` pins the
    complete certificate.  Reusing the value would produce an invalid Mihomo
    configuration, so Mihomo falls back to skipping certificate verification.
    """
    try:
        doc = json.loads(text)
    except Exception:
        return []
    if isinstance(doc, dict) and isinstance(doc.get("outbounds"), list):
        outbounds = doc["outbounds"]
    elif isinstance(doc, list):
        outbounds = doc
    elif isinstance(doc, dict):
        outbounds = [doc]
    else:
        return []

    warnings: list[str] = []
    for outbound in outbounds:
        if not isinstance(outbound, dict):
            continue
        tls = outbound.get("tls")
        pins = tls.get("certificate_public_key_sha256") if isinstance(tls, dict) else None
        if pins:
            name = str(outbound.get("tag") or outbound.get("server") or "该节点")
            warnings.append(
                f"{name}：sing-box 证书公钥 pin 无法等价转换为 Mihomo 指纹；"
                "为保证 Hysteria2 可连接，Mihomo 已设置 skip-cert-verify: true。"
            )
    return warnings


def parse_clash_yaml(text: str) -> list[ProxyNode]:
    return parse_yaml_proxies(text)
