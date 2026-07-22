"""FastAPI route definitions — mirrors src/http/api_server.cpp."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

import config_gen
import import_engine
import singbox_gen
from models import AppSettings, DeviceBinding, PortBinding, ProxyGroup, ProxyNode, Rule, RuleProvider, SubRuleEntry, TunnelEntry
from process_mgr import ProcessManager
from store import Store


def create_router(store: Store, proc_mgr: ProcessManager) -> APIRouter:
    router = APIRouter()

    # ── Status ─────────────────────────────────────────────────────────────

    @router.get("/api/status")
    def get_status() -> dict[str, Any]:
        s = store.get_settings()
        return {
            "mihomo_running": proc_mgr.running(),
            "mihomo_pid": proc_mgr.get_pid(),
            "sing_box_running": proc_mgr.sing_box_running(),
            "sing_box_pid": proc_mgr.get_sing_box_pid(),
            "mihomo_api_port": s.mihomo_api_port,
            "sing_box_api_port": s.sing_box_api_port,
            "management_port": s.management_port,
        }

    # ── Control ────────────────────────────────────────────────────────────

    @router.post("/api/start")
    def start_mihomo() -> dict[str, Any]:
        state = store.get_state()
        if proc_mgr.start(state):
            return {"ok": True}
        raise HTTPException(status_code=500, detail=proc_mgr.last_error or "Failed to start mihomo")

    @router.post("/api/stop")
    def stop_mihomo() -> dict[str, Any]:
        proc_mgr.stop()
        return {"ok": True}

    @router.post("/api/restart")
    def restart_mihomo() -> dict[str, Any]:
        state = store.get_state()
        if proc_mgr.restart(state):
            return {"ok": True}
        raise HTTPException(status_code=500, detail=proc_mgr.last_error or "Failed to restart mihomo")

    @router.post("/api/sing-box/start")
    def start_sing_box() -> dict[str, Any]:
        state = store.get_state()
        if proc_mgr.start_sing_box(state):
            return {"ok": True}
        raise HTTPException(status_code=500, detail=proc_mgr.sing_box_last_error or "Failed to start sing-box")

    @router.post("/api/sing-box/stop")
    def stop_sing_box() -> dict[str, Any]:
        proc_mgr.stop_sing_box()
        return {"ok": True}

    @router.post("/api/sing-box/restart")
    def restart_sing_box() -> dict[str, Any]:
        state = store.get_state()
        if proc_mgr.restart_sing_box(state):
            return {"ok": True}
        raise HTTPException(status_code=500, detail=proc_mgr.sing_box_last_error or "Failed to restart sing-box")

    # ── Logs ───────────────────────────────────────────────────────────────

    @router.get("/api/logs")
    def get_logs(n: int = 200) -> dict[str, Any]:
        return {"lines": proc_mgr.get_logs(n)}

    @router.delete("/api/logs")
    def clear_logs() -> dict[str, Any]:
        proc_mgr.clear_logs()
        return {"ok": True}

    @router.get("/api/sing-box/logs")
    def get_sing_box_logs(n: int = 200) -> dict[str, Any]:
        return {"lines": proc_mgr.get_sing_box_logs(n)}

    @router.delete("/api/sing-box/logs")
    def clear_sing_box_logs() -> dict[str, Any]:
        proc_mgr.clear_sing_box_logs()
        return {"ok": True}

    # ── Proxy Delay ─────────────────────────────────────────────────────

    @router.get("/api/proxy-delay/{name}")
    async def proxy_delay(name: str) -> dict[str, Any]:
        """Forward latency test request to Mihomo external controller."""
        s = store.get_settings()
        if not proc_mgr.running():
            raise HTTPException(status_code=409, detail="Mihomo 未运行")
        headers: dict[str, str] = {}
        if s.mihomo_api_secret:
            headers["Authorization"] = f"Bearer {s.mihomo_api_secret}"
        url = f"http://127.0.0.1:{s.mihomo_api_port}/proxies/{quote(name, safe='')}/delay"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url,
                    params={"timeout": "3000", "url": s.delay_test_url or "http://www.gstatic.com/generate_204"},
                    headers=headers,
                    timeout=5.0,
                )
            return resp.json()
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail="Mihomo API 无法连接") from exc

    # ── Connections ────────────────────────────────────────────────────────

    @router.get("/api/connections")
    async def get_connections() -> dict[str, Any]:
        """Forward connections request to Mihomo external controller."""
        s = store.get_settings()
        if not proc_mgr.running():
            raise HTTPException(status_code=409, detail="Mihomo 未运行")
        headers: dict[str, str] = {}
        if s.mihomo_api_secret:
            headers["Authorization"] = f"Bearer {s.mihomo_api_secret}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"http://127.0.0.1:{s.mihomo_api_port}/connections",
                    headers=headers,
                    timeout=3.0,
                )
            return resp.json()
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail="Mihomo API 无法连接") from exc

    @router.post("/api/reload")
    async def reload_mihomo() -> dict[str, Any]:
        return await _reload_mihomo_config()

    async def _reload_mihomo_config() -> dict[str, Any]:
        state = store.get_state()
        s = state.settings
        config_path = Path(s.data_dir) / "config.yaml"
        yaml_text = config_gen.generate(state)
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(yaml_text, encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to write config file: {exc}") from exc

        headers: dict[str, str] = {}
        if s.mihomo_api_secret:
            headers["Authorization"] = f"Bearer {s.mihomo_api_secret}"

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.put(
                    f"http://127.0.0.1:{s.mihomo_api_port}/configs?force=true",
                    json={"path": str(config_path)},
                    headers=headers,
                    timeout=3.0,
                )
            if resp.status_code == 204:
                return {"ok": True}
            raise HTTPException(
                status_code=502,
                detail=f"Mihomo reload failed (status {resp.status_code})",
            )
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail="Mihomo reload failed (connection refused)") from exc

    @router.post("/api/apply")
    async def apply_runtime_config(body: dict[str, Any]) -> dict[str, Any]:
        apply_mihomo = bool(body.get("mihomo", False))
        apply_sing_box = bool(body.get("sing_box", False))
        result: dict[str, Any] = {"mihomo": False, "sing_box": False}

        if apply_mihomo:
            await _reload_mihomo_config()
            result["mihomo"] = True
        if apply_sing_box:
            state = store.get_state()
            if not proc_mgr.restart_sing_box(state):
                raise HTTPException(status_code=500, detail=proc_mgr.sing_box_last_error or "Failed to apply port bindings")
            result["sing_box"] = True
        return {"ok": True, **result}

    # ── Config Preview ─────────────────────────────────────────────────────

    @router.get("/api/config/preview")
    def config_preview() -> PlainTextResponse:
        state = store.get_state()
        yaml_text = config_gen.generate(state)
        return PlainTextResponse(yaml_text, media_type="text/yaml")

    @router.get("/api/sing-box/config/preview")
    def sing_box_config_preview() -> PlainTextResponse:
        state = store.get_state()
        json_text = singbox_gen.generate(state)
        return PlainTextResponse(json_text, media_type="application/json")

    # ── Settings ───────────────────────────────────────────────────────────

    @router.get("/api/settings")
    def get_settings() -> AppSettings:
        return store.get_settings()

    @router.put("/api/settings")
    def put_settings(body: AppSettings) -> dict[str, Any]:
        store.update_settings(body)
        return {"ok": True}

    # ── Proxies ────────────────────────────────────────────────────────────

    @router.get("/api/proxies")
    def get_proxies() -> list[ProxyNode]:
        return store.get_proxies()

    @router.post("/api/proxies", status_code=201)
    def post_proxy(body: ProxyNode) -> ProxyNode:
        from models import generate_id
        body.id = generate_id()
        store.add_proxy(body)
        return body

    @router.put("/api/proxies/{pid}")
    def put_proxy(pid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_proxy(pid, body):
            raise HTTPException(status_code=404, detail="Proxy not found")
        return {"ok": True}

    @router.delete("/api/proxies/{pid}")
    def delete_proxy(pid: str) -> dict[str, Any]:
        if not store.delete_proxy(pid):
            raise HTTPException(status_code=404, detail="Proxy not found")
        return {"ok": True}

    # ── Proxy Groups ───────────────────────────────────────────────────────

    @router.get("/api/groups")
    def get_groups() -> list[ProxyGroup]:
        return store.get_groups()

    @router.post("/api/groups", status_code=201)
    def post_group(body: ProxyGroup) -> ProxyGroup:
        from models import generate_id
        body.id = generate_id()
        store.add_group(body)
        return body

    @router.put("/api/groups/{gid}")
    def put_group(gid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_group(gid, body):
            raise HTTPException(status_code=404, detail="Group not found")
        return {"ok": True}

    @router.delete("/api/groups/{gid}")
    def delete_group(gid: str) -> dict[str, Any]:
        if not store.delete_group(gid):
            raise HTTPException(status_code=404, detail="Group not found")
        return {"ok": True}

    # ── Rules ──────────────────────────────────────────────────────────────

    @router.get("/api/rules")
    def get_rules() -> list[Rule]:
        return store.get_rules()

    @router.post("/api/rules", status_code=201)
    def post_rule(body: dict[str, Any]) -> Rule:
        from models import generate_id
        r = Rule.model_validate(body)
        r.id = generate_id()
        prepend = bool(body.get("prepend", False))
        store.add_rule(r, prepend)
        return r

    @router.put("/api/rules/{rid}")
    def put_rule(rid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_rule(rid, body):
            raise HTTPException(status_code=404, detail="Rule not found")
        return {"ok": True}

    @router.delete("/api/rules/{rid}")
    def delete_rule(rid: str) -> dict[str, Any]:
        if not store.delete_rule(rid):
            raise HTTPException(status_code=404, detail="Rule not found")
        return {"ok": True}

    @router.post("/api/rules/reorder")
    def reorder_rules(body: list[str]) -> dict[str, Any]:
        store.reorder_rules(body)
        return {"ok": True}
    # ── Sub-rule sets ────────────────────────────────────────────────────

    @router.get("/api/sub-rules")
    def get_sub_rules() -> dict[str, list[SubRuleEntry]]:
        return store.get_sub_rule_sets()

    @router.put("/api/sub-rules/{name}")
    def put_sub_rule_set(name: str, body: list[dict[str, Any]]) -> dict[str, Any]:
        if "/" in name:
            raise HTTPException(status_code=400, detail="Set name cannot contain '/'")
        entries = [SubRuleEntry.model_validate(e) for e in body]
        store.set_sub_rule_set(name, entries)
        return {"ok": True}

    @router.post("/api/sub-rules/{name}/rename")
    def rename_sub_rule_set(name: str, body: dict[str, Any]) -> dict[str, Any]:
        new_name = str(body.get("new_name", "")).strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="new_name is required")
        if "/" in new_name:
            raise HTTPException(status_code=400, detail="Set name cannot contain '/'")
        if not store.rename_sub_rule_set(name, new_name):
            raise HTTPException(status_code=404, detail="Sub-rule set not found or name conflict")
        return {"ok": True}

    @router.delete("/api/sub-rules/{name}")
    def delete_sub_rule_set(name: str) -> dict[str, Any]:
        if not store.delete_sub_rule_set(name):
            raise HTTPException(status_code=404, detail="Sub-rule set not found")
        return {"ok": True}

    # ── Rule providers ──────────────────────────────────────────────────

    @router.get("/api/rule-providers")
    def get_rule_providers() -> dict[str, RuleProvider]:
        return store.get_rule_providers()

    @router.put("/api/rule-providers/{name}")
    def put_rule_provider(name: str, body: dict[str, Any]) -> dict[str, Any]:
        if "/" in name:
            raise HTTPException(status_code=400, detail="Name cannot contain '/'")
        rp = RuleProvider.model_validate(body)
        store.set_rule_provider(name, rp)
        return {"ok": True}

    @router.post("/api/rule-providers/{name}/rename")
    def rename_rule_provider(name: str, body: dict[str, Any]) -> dict[str, Any]:
        new_name = str(body.get("new_name", "")).strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="new_name is required")
        if "/" in new_name:
            raise HTTPException(status_code=400, detail="Name cannot contain '/'")
        if not store.rename_rule_provider(name, new_name):
            raise HTTPException(status_code=400, detail="原名称不存在或新名称已占用")
        return {"ok": True}

    @router.delete("/api/rule-providers/{name}")
    def delete_rule_provider(name: str) -> dict[str, Any]:
        if not store.delete_rule_provider(name):
            raise HTTPException(status_code=404, detail="规则集合不存在")
        return {"ok": True}

    # ── Import ─────────────────────────────────────────────────────────────

    @router.post("/api/import/uri")
    def import_uri(body: dict[str, Any]) -> dict[str, Any]:
        uri = str(body.get("uri", ""))
        nodes = import_engine.parse_uri(uri)
        if not nodes:
            raise HTTPException(status_code=400, detail="Failed to parse URI")
        store.add_proxies(nodes)
        return {"imported": len(nodes), "proxies": [n.model_dump() for n in nodes]}

    @router.post("/api/import/text")
    def import_text(body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text", ""))
        nodes = import_engine.parse_lines(text)
        if not nodes:
            nodes = import_engine.parse_base64_text(text)
        store.add_proxies(nodes)
        return {"imported": len(nodes), "proxies": [n.model_dump() for n in nodes]}

    @router.post("/api/import/subscription")
    def import_subscription(body: dict[str, Any]) -> dict[str, Any]:
        url = str(body.get("url", ""))
        nodes = import_engine.parse_subscription(url)
        if not nodes:
            raise HTTPException(status_code=400, detail="Failed to import subscription")
        store.add_proxies(nodes)
        return {"imported": len(nodes), "proxies": [n.model_dump() for n in nodes]}

    @router.post("/api/import/clash")
    def import_clash(body: dict[str, Any]) -> dict[str, Any]:
        yaml_text = str(body.get("yaml", ""))
        nodes = import_engine.parse_clash_yaml(yaml_text)
        if not nodes:
            raise HTTPException(status_code=400, detail="Failed to parse Clash config")
        store.add_proxies(nodes)
        return {"imported": len(nodes), "proxies": [n.model_dump() for n in nodes]}

    @router.post("/api/import/yaml")
    def import_yaml(body: dict[str, Any]) -> dict[str, Any]:
        yaml_text = str(body.get("yaml", ""))
        nodes = import_engine.parse_yaml_proxies(yaml_text)
        if not nodes:
            raise HTTPException(status_code=400, detail="未能解析任何代理配置")
        store.add_proxies(nodes)
        return {"imported": len(nodes), "proxies": [n.model_dump() for n in nodes]}

    @router.post("/api/import/sing-box")
    def import_singbox(body: dict[str, Any]) -> dict[str, Any]:
        json_text = str(body.get("json", ""))
        nodes = import_engine.parse_singbox_json(json_text)
        if not nodes:
            raise HTTPException(status_code=400, detail="未能解析任何 sing-box 出站配置")
        store.add_proxies(nodes)
        return {
            "imported": len(nodes),
            "proxies": [n.model_dump() for n in nodes],
            "warnings": import_engine.singbox_mihomo_compatibility_warnings(json_text),
        }

    # ── Device Bindings ─────────────────────────────────────────────────

    @router.get("/api/device-bindings")
    def get_device_bindings() -> list[DeviceBinding]:
        return store.get_device_bindings()

    @router.post("/api/device-bindings", status_code=201)
    def post_device_binding(body: DeviceBinding) -> DeviceBinding:
        from models import generate_id
        body.id = generate_id()
        store.add_device_binding(body)
        return body

    @router.put("/api/device-bindings/{bid}")
    def put_device_binding(bid: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_device_binding(bid, body):
            raise HTTPException(status_code=404, detail="Device binding not found")
        return {"ok": True}

    @router.delete("/api/device-bindings/{bid}")
    def delete_device_binding(bid: str) -> dict[str, Any]:
        if not store.delete_device_binding(bid):
            raise HTTPException(status_code=404, detail="Device binding not found")
        return {"ok": True}

    # ── Tunnels ───────────────────────────────────────────────────────────

    @router.get("/api/tunnels")
    def get_tunnels() -> list[TunnelEntry]:
        return store.get_tunnels()

    @router.post("/api/tunnels", status_code=201)
    def post_tunnel(body: TunnelEntry) -> TunnelEntry:
        from models import generate_id
        body.id = generate_id()
        store.add_tunnel(body)
        return body

    @router.put("/api/tunnels/{tunnel_id}")
    def put_tunnel(tunnel_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_tunnel(tunnel_id, body):
            raise HTTPException(status_code=404, detail="Tunnel not found")
        return {"ok": True}

    @router.delete("/api/tunnels/{tunnel_id}")
    def delete_tunnel(tunnel_id: str) -> dict[str, Any]:
        if not store.delete_tunnel(tunnel_id):
            raise HTTPException(status_code=404, detail="Tunnel not found")
        return {"ok": True}

    # ── sing-box Port Bindings ───────────────────────────────────────────

    @router.get("/api/port-bindings")
    def get_port_bindings() -> list[PortBinding]:
        return store.get_port_bindings()

    @router.post("/api/port-bindings", status_code=201)
    def post_port_binding(body: PortBinding) -> PortBinding:
        from models import generate_id
        body.id = generate_id()
        store.add_port_binding(body)
        return body

    @router.put("/api/port-bindings/{binding_id}")
    def put_port_binding(binding_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not store.update_port_binding(binding_id, body):
            raise HTTPException(status_code=404, detail="Port binding not found")
        return {"ok": True}

    @router.delete("/api/port-bindings/{binding_id}")
    def delete_port_binding(binding_id: str) -> dict[str, Any]:
        if not store.delete_port_binding(binding_id):
            raise HTTPException(status_code=404, detail="Port binding not found")
        return {"ok": True}

    # ── Close Connections ─────────────────────────────────────────────────

    @router.delete("/api/connections")
    async def close_all_connections() -> dict[str, Any]:
        s = store.get_settings()
        if not proc_mgr.running():
            raise HTTPException(status_code=409, detail="Mihomo 未运行")
        headers: dict[str, str] = {}
        if s.mihomo_api_secret:
            headers["Authorization"] = f"Bearer {s.mihomo_api_secret}"
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"http://127.0.0.1:{s.mihomo_api_port}/connections",
                    headers=headers,
                    timeout=3.0,
                )
            return {"ok": True}
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail="Mihomo API 无法连接") from exc

    @router.delete("/api/connections/{cid}")
    async def close_connection(cid: str) -> dict[str, Any]:
        s = store.get_settings()
        if not proc_mgr.running():
            raise HTTPException(status_code=409, detail="Mihomo 未运行")
        headers: dict[str, str] = {}
        if s.mihomo_api_secret:
            headers["Authorization"] = f"Bearer {s.mihomo_api_secret}"
        try:
            async with httpx.AsyncClient() as client:
                await client.delete(
                    f"http://127.0.0.1:{s.mihomo_api_port}/connections/{quote(cid, safe='')}",
                    headers=headers,
                    timeout=3.0,
                )
            return {"ok": True}
        except httpx.ConnectError as exc:
            raise HTTPException(status_code=502, detail="Mihomo API 无法连接") from exc

    # ── Dashboard entries / reverse proxies ──────────────────────────────

    async def proxy_dashboard(path: str, request: Request, api_port: int, name: str) -> Response:
        """Serve a core's bundled Clash dashboard through the management port."""
        upstream = f"http://127.0.0.1:{api_port}/ui/{path}"
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"

        passthrough_headers: dict[str, str] = {}
        for key in ("accept", "accept-encoding", "accept-language", "cache-control", "pragma", "user-agent"):
            value = request.headers.get(key)
            if value:
                passthrough_headers[key] = value

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                resp = await client.request(request.method, upstream, headers=passthrough_headers)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"{name} dashboard unavailable: {exc}") from exc

        excluded = {
            "content-length", "transfer-encoding", "connection", "keep-alive",
            "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
        }
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    def dashboard_entry(title: str, endpoint_id: str, endpoint_url: str, target: str) -> str:
        return f"""<!doctype html>
<html lang=\"zh-CN\">
<head><meta charset=\"utf-8\"><title>{title}</title></head>
<body>
<script>
(function () {{
  var endpointId = {json.dumps(endpoint_id)};
  var endpoint = {{ id: endpointId, url: {json.dumps(endpoint_url)}, secret: '' }};
  var endpointList = [];
  try {{
    endpointList = JSON.parse(localStorage.getItem('endpointList') || '[]');
    if (!Array.isArray(endpointList)) endpointList = [];
  }} catch (_) {{ endpointList = []; }}
  endpointList = endpointList.filter(function (item) {{ return item && item.id !== endpointId; }});
  endpointList.unshift(endpoint);
  localStorage.setItem('endpointList', JSON.stringify(endpointList));
  localStorage.setItem('selectedEndpoint', endpointId);
  location.replace({json.dumps(target)});
}})();
</script>
</body>
</html>"""

    # ── Mihomo MetaCubeXD ────────────────────────────────────────────────

    @router.get("/metacubexd-entry", response_class=HTMLResponse)
    def metacubexd_entry() -> str:
        s = store.get_settings()
        # The UI runs behind this application's reverse proxy; preserve the
        # actual Mihomo API URL and optional secret for MetaCubeXD.
        port = int(s.mihomo_api_port)
        secret_js = json.dumps(s.mihomo_api_secret or "")
        return f"""<!doctype html><script>(function(){{
var id='router-manager', host=location.hostname||'127.0.0.1', list=[];
try{{list=JSON.parse(localStorage.getItem('endpointList')||'[]')}}catch(_){{}}
if(!Array.isArray(list))list=[];
list=list.filter(function(x){{return x&&x.id!==id}});
list.unshift({{id:id,url:'http://'+host+':{port}',secret:{secret_js}}});
localStorage.setItem('endpointList',JSON.stringify(list)); localStorage.setItem('selectedEndpoint',id);
location.replace('/metacubexd/')}})();</script>"""

    @router.get("/metacubexd")
    def metacubexd_root() -> RedirectResponse:
        return RedirectResponse(url="/metacubexd/", status_code=307)

    @router.api_route("/metacubexd/{path:path}", methods=["GET", "HEAD"])
    async def metacubexd_proxy(path: str, request: Request) -> Response:
        s = store.get_settings()
        return await proxy_dashboard(path, request, s.mihomo_api_port, "MetaCubeXD")

    # ── sing-box dashboard (MetaCubeXD using sing-box's Clash API) ───────

    @router.get("/sing-box-dashboard-entry", response_class=HTMLResponse)
    def sing_box_dashboard_entry() -> str:
        return dashboard_entry(
            "sing-box Dashboard",
            "router-manager-sing-box",
            "/sing-box-api",
            "/sing-box-dashboard/",
        )

    @router.get("/sing-box-dashboard")
    def sing_box_dashboard_root() -> RedirectResponse:
        return RedirectResponse(url="/sing-box-dashboard/", status_code=307)

    @router.api_route("/sing-box-dashboard/{path:path}", methods=["GET", "HEAD"])
    async def sing_box_dashboard_proxy(path: str, request: Request) -> Response:
        s = store.get_settings()
        if not proc_mgr.sing_box_running():
            raise HTTPException(status_code=409, detail="sing-box 未运行")
        return await proxy_dashboard(path, request, s.sing_box_api_port, "sing-box")

    @router.api_route("/sing-box-api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def sing_box_api_proxy(path: str, request: Request) -> Response:
        """Expose sing-box's Clash API only through the management origin."""
        s = store.get_settings()
        if not proc_mgr.sing_box_running():
            raise HTTPException(status_code=409, detail="sing-box 未运行")
        upstream = f"http://127.0.0.1:{s.sing_box_api_port}/{path}"
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"
        headers = {
            key: value for key in ("accept", "content-type")
            if (value := request.headers.get(key))
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                resp = await client.request(request.method, upstream, headers=headers, content=await request.body())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"sing-box API unavailable: {exc}") from exc
        excluded = {"content-length", "transfer-encoding", "connection", "keep-alive"}
        out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    return router
