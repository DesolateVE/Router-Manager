"""Mihomo config YAML generator — mirrors src/core/config_gen.cpp."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from models import AppState, AppSettings, ProxyGroup, ProxyNode, Rule, RuleProvider, SubRuleEntry


# ── Helpers ────────────────────────────────────────────────────────────────

def _resolve_ref(state: AppState, ref: str, id_to_name: dict[str, str]) -> str:
    if ref in ("DIRECT", "REJECT", "PASS"):
        return ref
    if ref in id_to_name:
        return id_to_name[ref]
    for g in state.groups:
        if g.id == ref or g.name == ref:
            return g.name
    # Fallback: look up proxy node by original name or alias (alias is display-only)
    for p in state.proxies:
        if p.enabled and (p.name == ref or (p.alias and p.alias == ref)):
            return p.name
    return ref


def _rule_str(r: Rule | SubRuleEntry, state: AppState, id_to_name: dict[str, str]) -> str:
    if r.type == "MATCH":
        target = _resolve_ref(state, r.target, id_to_name)
        return f"MATCH,{target}"
    if r.type == "SUB-RULE":
        # payload stores the condition without outer parens, e.g. "NETWORK,tcp"
        # target stores the destination sub-rule set name (never resolved through proxy map)
        return f"SUB-RULE,({r.payload}),{r.target}"
    target = _resolve_ref(state, r.target, id_to_name)
    s = f"{r.type},{r.payload},{target}"
    if r.no_resolve:
        s += ",no-resolve"
    return s


def _build_proxy(p: ProxyNode, name: str, state: AppState, id_to_name: dict[str, str]) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": name,
        "type": p.type,
        "server": p.server,
        "port": p.port,
    }
    d.update(p.extra)
    # dialer-proxy must reference the real node/group name, not an alias
    if "dialer-proxy" in d:
        d["dialer-proxy"] = _resolve_ref(state, str(d["dialer-proxy"]), id_to_name)
    return d


def _build_group(
    g: ProxyGroup,
    state: AppState,
    enabled: list[ProxyNode],
    id_to_name: dict[str, str],
) -> dict[str, Any]:
    d: dict[str, Any] = {"name": g.name, "type": g.type}

    if g.include_all and enabled:
        proxies = ["DIRECT", "REJECT"] + [id_to_name[p.id] for p in enabled]
    else:
        proxies = []
        for ref in g.proxies:
            resolved = _resolve_ref(state, ref, id_to_name)
            if resolved:
                proxies.append(resolved)

    if proxies:
        d["proxies"] = proxies

    if g.type in ("url-test", "fallback", "load-balance"):
        d["url"] = g.url
        d["interval"] = g.interval
        d["timeout"] = g.timeout

    return d




# ── Public API ─────────────────────────────────────────────────────────────

def generate(state: AppState) -> str:
    # 1. Load template
    tpl_path = Path(state.settings.data_dir) / "template.yaml"
    if tpl_path.exists():
        try:
            cfg: dict[str, Any] = yaml.safe_load(tpl_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    else:
        cfg = {}

    s = state.settings

    # 2. Override common fields
    cfg["mixed-port"] = s.mixed_port
    cfg["allow-lan"] = s.allow_lan
    cfg["mode"] = s.mode
    cfg["log-level"] = s.log_level
    cfg["ipv6"] = s.ipv6
    cfg["find-process-mode"] = s.find_process_mode
    cfg["external-controller"] = f"0.0.0.0:{s.mihomo_api_port}"
    if s.mihomo_api_secret:
        cfg["secret"] = s.mihomo_api_secret
    else:
        cfg.pop("secret", None)

    # 3. Build proxy name map (deduplicate)
    enabled_proxies = [p for p in state.proxies if p.enabled]
    used_names: set[str] = set()
    id_to_name: dict[str, str] = {}
    for p in enabled_proxies:
        base = p.name  # alias is display-only; Mihomo config always uses original name
        name = base
        i = 1
        while name in used_names:
            name = f"{base}_{i}"
            i += 1
        used_names.add(name)
        id_to_name[p.id] = name

    # 5. proxies block
    if enabled_proxies:
        cfg["proxies"] = [_build_proxy(p, id_to_name[p.id], state, id_to_name) for p in enabled_proxies]
    else:
        cfg.pop("proxies", None)

    # 6. proxy-groups block
    if state.groups:
        cfg["proxy-groups"] = [
            _build_group(g, state, enabled_proxies, id_to_name) for g in state.groups
        ]
    else:
        cfg.pop("proxy-groups", None)

    # 7. rule-providers block
    if state.rule_providers:
        rp_out: dict[str, Any] = {}
        for name, rp in state.rule_providers.items():
            d: dict[str, Any] = {"behavior": rp.behavior, "type": rp.type}
            if rp.type == "http" and rp.url:
                d["url"] = rp.url
                d["interval"] = rp.interval
            if rp.path:
                d["path"] = rp.path
            if rp.format and rp.format != "yaml":
                d["format"] = rp.format
            rp_out[name] = d
        cfg["rule-providers"] = rp_out
    else:
        cfg.pop("rule-providers", None)

    # 8. sub-rules block
    if state.sub_rule_sets:
        sub_rules_out: dict[str, list[str]] = {}
        for name, entries in state.sub_rule_sets.items():
            rule_strs = [_rule_str(e, state, id_to_name) for e in entries if e.enabled]
            if rule_strs:
                sub_rules_out[name] = rule_strs
        if sub_rules_out:
            cfg["sub-rules"] = sub_rules_out
        else:
            cfg.pop("sub-rules", None)
    else:
        cfg.pop("sub-rules", None)

    # 8. Device binding rules (prepend SRC-IP-CIDR rules for bound devices)
    binding_rule_strs = []
    for b in state.device_bindings:
        if not b.enabled or not b.ip or not b.proxy:
            continue
        cidr = b.ip if '/' in b.ip else b.ip + '/32'
        target = _resolve_ref(state, b.proxy, id_to_name)
        binding_rule_strs.append(f"SRC-IP-CIDR,{cidr},{target},no-resolve")

    # 8. rules block
    enabled_rules = [r for r in state.rules if r.enabled]
    all_rule_strs = binding_rule_strs + [_rule_str(r, state, id_to_name) for r in enabled_rules]
    if all_rule_strs:
        cfg["rules"] = all_rule_strs
    else:
        cfg.pop("rules", None)

    # 8. Emit YAML
    return yaml.dump(cfg, allow_unicode=True, sort_keys=False, default_flow_style=False)
