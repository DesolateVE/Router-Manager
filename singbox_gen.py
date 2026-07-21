"""sing-box JSON config generator for fixed local port bindings."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from models import AppState, PortBinding, ProxyNode


def _display_name(proxy: ProxyNode) -> str:
    return proxy.alias or proxy.name or proxy.id


def _tls_from_extra(extra: dict[str, Any]) -> dict[str, Any] | None:
    tls: dict[str, Any] = {}
    server_name = extra.get("sni") or extra.get("servername")
    if server_name:
        tls["server_name"] = server_name
    if extra.get("alpn"):
        tls["alpn"] = extra["alpn"]
    if "skip-cert-verify" in extra:
        tls["insecure"] = bool(extra["skip-cert-verify"])
    fingerprint = extra.get("client-fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    reality_opts = extra.get("reality-opts")
    if isinstance(reality_opts, dict):
        reality: dict[str, Any] = {"enabled": True}
        if reality_opts.get("public-key"):
            reality["public_key"] = reality_opts["public-key"]
        if reality_opts.get("short-id"):
            reality["short_id"] = reality_opts["short-id"]
        tls["reality"] = reality
    if tls or extra.get("tls") is True:
        tls["enabled"] = True
        return tls
    return None


def _transport_from_extra(extra: dict[str, Any]) -> dict[str, Any] | None:
    network = extra.get("network")
    if network == "ws":
        ws_opts = extra.get("ws-opts") if isinstance(extra.get("ws-opts"), dict) else {}
        transport: dict[str, Any] = {"type": "ws"}
        if ws_opts.get("path"):
            transport["path"] = ws_opts["path"]
        if isinstance(ws_opts.get("headers"), dict) and ws_opts["headers"]:
            transport["headers"] = ws_opts["headers"]
        return transport
    if network == "grpc":
        grpc_opts = extra.get("grpc-opts") if isinstance(extra.get("grpc-opts"), dict) else {}
        transport = {"type": "grpc"}
        service_name = grpc_opts.get("grpc-service-name")
        if service_name:
            transport["service_name"] = service_name
        return transport
    return None


def _build_outbound(proxy: ProxyNode, tag: str) -> dict[str, Any]:
    raw = proxy.extra.get("sing-box-outbound")
    if isinstance(raw, dict):
        outbound = deepcopy(raw)
        outbound["tag"] = tag
        return outbound

    extra = proxy.extra or {}
    type_map = {
        "ss": "shadowsocks",
        "socks5": "socks",
        "http": "http",
        "vmess": "vmess",
        "vless": "vless",
        "trojan": "trojan",
        "hysteria2": "hysteria2",
        "tuic": "tuic",
    }
    outbound: dict[str, Any] = {
        "type": type_map.get(proxy.type, proxy.type),
        "tag": tag,
        "server": proxy.server,
        "server_port": proxy.port,
    }
    if proxy.type == "ss":
        if extra.get("cipher"):
            outbound["method"] = extra["cipher"]
        if extra.get("password"):
            outbound["password"] = extra["password"]
    elif proxy.type in ("vmess", "vless"):
        if extra.get("uuid"):
            outbound["uuid"] = extra["uuid"]
        if proxy.type == "vmess" and extra.get("cipher"):
            outbound["security"] = extra["cipher"]
        if proxy.type == "vmess" and "alterId" in extra:
            outbound["alter_id"] = extra["alterId"]
        if proxy.type == "vless" and extra.get("flow"):
            outbound["flow"] = extra["flow"]
    elif proxy.type in ("trojan", "hysteria2", "tuic"):
        if extra.get("password"):
            outbound["password"] = extra["password"]
        if proxy.type == "tuic" and extra.get("uuid"):
            outbound["uuid"] = extra["uuid"]
    elif proxy.type in ("http", "socks5"):
        if extra.get("username"):
            outbound["username"] = extra["username"]
        if extra.get("password"):
            outbound["password"] = extra["password"]

    if proxy.type == "hysteria2":
        if extra.get("obfs"):
            outbound["obfs"] = extra["obfs"]
        if extra.get("obfs-password"):
            outbound["obfs_password"] = extra["obfs-password"]

    tls = _tls_from_extra(extra)
    if tls:
        outbound["tls"] = tls
    transport = _transport_from_extra(extra)
    if transport:
        outbound["transport"] = transport
    return outbound


def generate(state: AppState) -> str:
    enabled_proxies = {p.id: p for p in state.proxies if p.enabled}
    bindings = [b for b in state.port_bindings if b.enabled and b.port > 0 and b.proxy in enabled_proxies]

    inbounds: list[dict[str, Any]] = []
    outbounds: list[dict[str, Any]] = [{"type": "direct", "tag": "direct"}]
    rules: list[dict[str, Any]] = []
    outbound_tags: set[str] = {"direct"}

    for binding in bindings:
        inbound_tag = f"in-{binding.id}"
        outbound_tag = f"out-{binding.proxy}"
        inbounds.append({
            "type": binding.inbound_type or "mixed",
            "tag": inbound_tag,
            "listen": binding.listen or "0.0.0.0",
            "listen_port": binding.port,
        })
        if outbound_tag not in outbound_tags:
            outbounds.append(_build_outbound(enabled_proxies[binding.proxy], outbound_tag))
            outbound_tags.add(outbound_tag)
        rules.append({"inbound": [inbound_tag], "outbound": outbound_tag})

    config = {
        "log": {"level": state.settings.log_level or "info"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules, "final": "direct"},
    }
    return json.dumps(config, ensure_ascii=False, indent=2)


def binding_summary(binding: PortBinding, proxy: ProxyNode | None) -> str:
    target = _display_name(proxy) if proxy else "未选择节点"
    return f"{binding.listen}:{binding.port} -> {target}"