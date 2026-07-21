"""Data models for Router Manager — mirrors src/core/types.hpp."""
from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


def generate_id() -> str:
    return uuid.uuid4().hex[:16]


class ProxyNode(BaseModel):
    id: str = Field(default_factory=generate_id)
    name: str = ""
    alias: str = ""
    type: str = ""
    server: str = ""
    port: int = 0
    enabled: bool = True
    extra: dict[str, Any] = Field(default_factory=dict)

    def display_name(self) -> str:
        return self.alias if self.alias else self.name


class ProxyGroup(BaseModel):
    id: str = Field(default_factory=generate_id)
    name: str = ""
    type: str = "select"
    proxies: list[str] = Field(default_factory=list)
    url: str = "http://www.gstatic.com/generate_204"
    interval: int = 300
    timeout: int = 5000
    include_all: bool = False


class SubRuleEntry(BaseModel):
    """One rule entry inside a named sub-rule set."""
    type: str = ""
    payload: str = ""     # For SUB-RULE: "NETWORK,tcp" (condition, no outer parens)
    target: str = ""      # For SUB-RULE: destination set name; else proxy/group ref
    no_resolve: bool = False
    enabled: bool = True


class Rule(BaseModel):
    id: str = Field(default_factory=generate_id)
    type: str = ""
    payload: str = ""     # For SUB-RULE: "NETWORK,tcp" (condition, no outer parens)
    target: str = ""      # For SUB-RULE: destination set name; else proxy/group ref
    no_resolve: bool = False
    enabled: bool = True


class AppSettings(BaseModel):
    mixed_port: int = 7890
    allow_lan: bool = True
    mode: str = "rule"
    log_level: str = "info"
    mihomo_api_port: int = 9090
    mihomo_api_secret: str = ""
    management_port: int = 9080
    mihomo_bin: str = "/etc/router_manager/mihomo"
    sing_box_bin: str = "/usr/bin/sing-box"
    data_dir: str = "/etc/router_manager"
    ipv6: bool = False
    find_process_mode: str = "off"
    delay_test_url: str = "http://www.gstatic.com/generate_204"


class RuleProvider(BaseModel):
    behavior: str = "domain"  # domain | ipcidr | classical
    type: str = "http"         # http | file
    url: str = ""
    path: str = ""
    interval: int = 86400
    format: str = "yaml"       # yaml | mrs | text


class DeviceBinding(BaseModel):
    id: str = Field(default_factory=generate_id)
    label: str = ""         # friendly name, e.g. "Xbox One", "小明的 PC"
    ip: str = ""             # source IP, e.g. "192.168.1.100" (/32 implied)
    proxy: str = ""          # proxy/group name to bind to
    enabled: bool = True


class TunnelEntry(BaseModel):
    id: str = Field(default_factory=generate_id)
    label: str = ""
    network: list[str] = Field(default_factory=lambda: ["tcp"])
    address: str = ""
    target: str = ""
    proxy: str = ""
    enabled: bool = True


class PortBinding(BaseModel):
    id: str = Field(default_factory=generate_id)
    label: str = ""
    listen: str = "0.0.0.0"
    port: int = 7901
    inbound_type: str = "mixed"
    proxy: str = ""
    enabled: bool = True


class AppState(BaseModel):
    settings: AppSettings = Field(default_factory=AppSettings)
    proxies: list[ProxyNode] = Field(default_factory=list)
    groups: list[ProxyGroup] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list)
    sub_rule_sets: dict[str, list[SubRuleEntry]] = Field(default_factory=dict)
    rule_providers: dict[str, RuleProvider] = Field(default_factory=dict)
    device_bindings: list[DeviceBinding] = Field(default_factory=list)
    tunnels: list[TunnelEntry] = Field(default_factory=list)
    port_bindings: list[PortBinding] = Field(default_factory=list)
