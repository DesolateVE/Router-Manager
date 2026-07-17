"""Thread-safe JSON persistence — mirrors src/core/store.cpp."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from models import AppSettings, AppState, DeviceBinding, ProxyGroup, ProxyNode, Rule, RuleProvider, SubRuleEntry, TunnelEntry, generate_id


class Store:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._state = AppState()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def load(self) -> bool:
        with self._lock:
            if not self._path.exists():
                self._state = self._create_defaults()
                return self._save_unlocked()
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._state = AppState.model_validate(data)
                return True
            except Exception:
                self._state = self._create_defaults()
                return True

    def get_state(self) -> AppState:
        with self._lock:
            return self._state.model_copy(deep=True)

    # ── Settings ───────────────────────────────────────────────────────────

    def get_settings(self) -> AppSettings:
        with self._lock:
            return self._state.settings.model_copy(deep=True)

    def update_settings(self, s: AppSettings) -> None:
        with self._lock:
            self._state.settings = s
            self._save_unlocked()

    # ── Proxies ────────────────────────────────────────────────────────────

    def get_proxies(self) -> list[ProxyNode]:
        with self._lock:
            return list(self._state.proxies)

    def add_proxy(self, p: ProxyNode) -> None:
        with self._lock:
            if not p.id:
                p.id = generate_id()
            self._state.proxies.append(p)
            self._save_unlocked()

    def add_proxies(self, nodes: list[ProxyNode]) -> None:
        with self._lock:
            for p in nodes:
                if not p.id:
                    p.id = generate_id()
                self._state.proxies.append(p)
            self._save_unlocked()

    def update_proxy(self, pid: str, patch: dict[str, Any]) -> bool:
        with self._lock:
            p = self._find_proxy(pid)
            if p is None:
                return False
            for field in ("name", "alias", "type", "server", "port", "enabled", "extra"):
                if field in patch:
                    setattr(p, field, patch[field])
            self._save_unlocked()
            return True

    def delete_proxy(self, pid: str) -> bool:
        with self._lock:
            before = len(self._state.proxies)
            self._state.proxies = [p for p in self._state.proxies if p.id != pid]
            if len(self._state.proxies) == before:
                return False
            # Remove from all groups
            for g in self._state.groups:
                g.proxies = [ref for ref in g.proxies if ref != pid]
            self._save_unlocked()
            return True

    # ── Groups ─────────────────────────────────────────────────────────────

    def get_groups(self) -> list[ProxyGroup]:
        with self._lock:
            return list(self._state.groups)

    def add_group(self, g: ProxyGroup) -> None:
        with self._lock:
            if not g.id:
                g.id = generate_id()
            self._state.groups.append(g)
            self._save_unlocked()

    def update_group(self, gid: str, patch: dict[str, Any]) -> bool:
        with self._lock:
            g = self._find_group(gid)
            if g is None:
                return False
            for field in ("name", "type", "url", "interval", "timeout", "include_all"):
                if field in patch:
                    setattr(g, field, patch[field])
            if "proxies" in patch:
                g.proxies = list(patch["proxies"])
            self._save_unlocked()
            return True

    def delete_group(self, gid: str) -> bool:
        with self._lock:
            before = len(self._state.groups)
            self._state.groups = [g for g in self._state.groups if g.id != gid]
            if len(self._state.groups) == before:
                return False
            self._save_unlocked()
            return True

    # ── Rules ──────────────────────────────────────────────────────────────

    def get_rules(self) -> list[Rule]:
        with self._lock:
            return list(self._state.rules)

    def add_rule(self, r: Rule, prepend: bool = False) -> None:
        with self._lock:
            if not r.id:
                r.id = generate_id()
            if prepend:
                self._state.rules.insert(0, r)
            else:
                self._state.rules.append(r)
            self._save_unlocked()

    def update_rule(self, rid: str, patch: dict[str, Any]) -> bool:
        with self._lock:
            r = self._find_rule(rid)
            if r is None:
                return False
            for field in ("type", "payload", "target", "no_resolve", "enabled"):
                if field in patch:
                    setattr(r, field, patch[field])
            self._save_unlocked()
            return True

    def delete_rule(self, rid: str) -> bool:
        with self._lock:
            before = len(self._state.rules)
            self._state.rules = [r for r in self._state.rules if r.id != rid]
            if len(self._state.rules) == before:
                return False
            self._save_unlocked()
            return True

    def reorder_rules(self, ids: list[str]) -> None:
        with self._lock:
            id_set = set(ids)
            ordered: list[Rule] = []
            rule_map = {r.id: r for r in self._state.rules}
            for rid in ids:
                if rid in rule_map:
                    ordered.append(rule_map[rid])
            # Append any rules not in the ID list
            for r in self._state.rules:
                if r.id not in id_set:
                    ordered.append(r)
            self._state.rules = ordered
            self._save_unlocked()

    # ── Sub-rule sets ──────────────────────────────────────────────────────

    def get_sub_rule_sets(self) -> dict[str, list[SubRuleEntry]]:
        with self._lock:
            return {k: list(v) for k, v in self._state.sub_rule_sets.items()}

    def set_sub_rule_set(self, name: str, entries: list[SubRuleEntry]) -> None:
        with self._lock:
            self._state.sub_rule_sets[name] = entries
            self._save_unlocked()

    def rename_sub_rule_set(self, old_name: str, new_name: str) -> bool:
        with self._lock:
            if old_name not in self._state.sub_rule_sets:
                return False
            if new_name != old_name and new_name in self._state.sub_rule_sets:
                return False
            entries = self._state.sub_rule_sets.pop(old_name)
            self._state.sub_rule_sets[new_name] = entries
            self._save_unlocked()
            return True

    def delete_sub_rule_set(self, name: str) -> bool:
        with self._lock:
            if name not in self._state.sub_rule_sets:
                return False
            del self._state.sub_rule_sets[name]
            self._save_unlocked()
            return True

    # ── Private helpers ────────────────────────────────────────────────────

    def _find_proxy(self, pid: str) -> ProxyNode | None:
        for p in self._state.proxies:
            if p.id == pid:
                return p
        return None

    def _find_group(self, gid: str) -> ProxyGroup | None:
        for g in self._state.groups:
            if g.id == gid:
                return g
        return None

    def _find_rule(self, rid: str) -> Rule | None:
        for r in self._state.rules:
            if r.id == rid:
                return r
        return None

    def _save_unlocked(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._state.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    # ── Rule providers ───────────────────────────────────────────────────

    def get_rule_providers(self) -> dict[str, RuleProvider]:
        with self._lock:
            return dict(self._state.rule_providers)

    def set_rule_provider(self, name: str, rp: RuleProvider) -> None:
        with self._lock:
            self._state.rule_providers[name] = rp
            self._save_unlocked()

    def rename_rule_provider(self, old_name: str, new_name: str) -> bool:
        with self._lock:
            if old_name not in self._state.rule_providers:
                return False
            if new_name != old_name and new_name in self._state.rule_providers:
                return False
            rp = self._state.rule_providers.pop(old_name)
            self._state.rule_providers[new_name] = rp
            self._save_unlocked()
            return True

    def delete_rule_provider(self, name: str) -> bool:
        with self._lock:
            if name not in self._state.rule_providers:
                return False
            del self._state.rule_providers[name]
            self._save_unlocked()
            return True

    # ── Device Bindings ───────────────────────────────────────────────────

    def get_device_bindings(self) -> list[DeviceBinding]:
        with self._lock:
            return list(self._state.device_bindings)

    def add_device_binding(self, b: DeviceBinding) -> None:
        with self._lock:
            if not b.id:
                b.id = generate_id()
            self._state.device_bindings.append(b)
            self._save_unlocked()

    def update_device_binding(self, bid: str, patch: dict[str, Any]) -> bool:
        with self._lock:
            b = next((x for x in self._state.device_bindings if x.id == bid), None)
            if b is None:
                return False
            for field in ("label", "ip", "proxy", "enabled"):
                if field in patch:
                    setattr(b, field, patch[field])
            self._save_unlocked()
            return True

    def delete_device_binding(self, bid: str) -> bool:
        with self._lock:
            before = len(self._state.device_bindings)
            self._state.device_bindings = [b for b in self._state.device_bindings if b.id != bid]
            if len(self._state.device_bindings) == before:
                return False
            self._save_unlocked()
            return True

    # ── Tunnels ───────────────────────────────────────────────────────────

    def get_tunnels(self) -> list[TunnelEntry]:
        with self._lock:
            return list(self._state.tunnels)

    def add_tunnel(self, tunnel: TunnelEntry) -> None:
        with self._lock:
            if not tunnel.id:
                tunnel.id = generate_id()
            self._state.tunnels.append(tunnel)
            self._save_unlocked()

    def update_tunnel(self, tunnel_id: str, patch: dict[str, Any]) -> bool:
        with self._lock:
            tunnel = next((x for x in self._state.tunnels if x.id == tunnel_id), None)
            if tunnel is None:
                return False
            for field in ("label", "address", "target", "proxy", "enabled"):
                if field in patch:
                    setattr(tunnel, field, patch[field])
            if "network" in patch:
                tunnel.network = list(patch["network"])
            self._save_unlocked()
            return True

    def delete_tunnel(self, tunnel_id: str) -> bool:
        with self._lock:
            before = len(self._state.tunnels)
            self._state.tunnels = [t for t in self._state.tunnels if t.id != tunnel_id]
            if len(self._state.tunnels) == before:
                return False
            self._save_unlocked()
            return True

    @staticmethod
    def _create_defaults() -> AppState:
        st = AppState()
        # Default rules
        r1 = Rule(id=generate_id(), type="GEOIP", payload="CN", target="DIRECT")
        r2 = Rule(id=generate_id(), type="MATCH", payload="", target="DIRECT")
        st.rules.extend([r1, r2])
        return st
