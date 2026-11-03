"""Regression coverage for installed versions and independent dual-core telemetry."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import create_router
from models import AppSettings
from process_mgr import ProcessManager
from store import Store


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.binary = Path(self.temp.name) / "core"
        self.binary.write_text("first binary", encoding="utf-8")
        self.manager = ProcessManager()

    def test_versions_work_for_stopped_cores_and_cache_until_binary_changes(self):
        with patch("process_mgr.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "Mihomo Meta v1.19.15 linux amd64\n")
            self.assertFalse(self.manager.running())
            self.assertEqual(self.manager.get_version("mihomo", str(self.binary)), "v1.19.15")
            self.assertEqual(self.manager.get_version("mihomo", str(self.binary)), "v1.19.15")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], [str(self.binary), "-v"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2.0)

            self.binary.write_text("updated binary with a different size", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess([], 0, "Mihomo Meta v1.20.0 linux amd64\n")
            self.assertEqual(self.manager.get_version("mihomo", str(self.binary)), "v1.20.0")
            self.assertEqual(run.call_count, 2)

    def test_sing_box_multiline_version_and_configured_path_change(self):
        with patch("process_mgr.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "sing-box version 1.12.9\n\nEnvironment: go1.25\nTags: with_clash_api\n")
            self.assertEqual(self.manager.get_version("sing_box", str(self.binary)), "1.12.9")
            self.assertEqual(run.call_args.args[0][-1], "version")
            replacement = Path(self.temp.name) / "other-core"
            replacement.write_text("another core", encoding="utf-8")
            run.return_value = subprocess.CompletedProcess([], 0, "sing-box version 1.13.0-beta.1\n")
            self.assertEqual(self.manager.get_version("sing_box", str(replacement)), "1.13.0-beta.1")
            self.assertEqual(run.call_count, 2)

    def test_missing_timeout_nonzero_and_invalid_output_are_safe(self):
        for failure in (FileNotFoundError(), subprocess.TimeoutExpired("core", 2), subprocess.CalledProcessError(1, "core")):
            with self.subTest(failure=type(failure).__name__):
                manager = ProcessManager()
                with patch("process_mgr.subprocess.run", side_effect=failure) as run:
                    self.assertIsNone(manager.get_version("mihomo", str(self.binary)))
                    self.assertIsNone(manager.get_version("mihomo", str(self.binary)))
                    self.assertEqual(run.call_count, 1)
        with patch("process_mgr.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "not a version")):
            self.assertIsNone(ProcessManager().get_version("mihomo", str(self.binary)))

    def test_failure_cache_expires_for_recovery(self):
        with patch("process_mgr.time.monotonic", return_value=10) as clock, patch("process_mgr.subprocess.run") as run:
            run.side_effect = FileNotFoundError()
            self.assertIsNone(self.manager.get_version("mihomo", str(self.binary)))
            clock.return_value = 41
            run.side_effect = None
            run.return_value = subprocess.CompletedProcess([], 0, "Mihomo Meta v1.20.0 linux amd64\n")
            self.assertEqual(self.manager.get_version("mihomo", str(self.binary)), "v1.20.0")
            self.assertEqual(run.call_count, 2)


class ProcessMemoryTests(unittest.TestCase):
    def test_managed_pid_resident_kilobytes_are_converted_to_bytes(self):
        manager = ProcessManager()
        status = "Name:\tmihomo\nVmSize:\t900000 kB\nVmRSS:\t76452 kB\nRssAnon:\t70000 kB\n"
        with patch.object(manager, "get_pid", return_value=123), patch.object(manager, "get_sing_box_pid", return_value=456):
            with patch("process_mgr.Path.read_text", autospec=True, return_value=status) as read:
                self.assertEqual(manager.get_memory("mihomo"), 76452 * 1024)
                self.assertEqual(read.call_args.args[0].as_posix(), "/proc/123/status")
                self.assertEqual(manager.get_memory("sing_box"), 76452 * 1024)
                self.assertEqual(read.call_args.args[0].as_posix(), "/proc/456/status")

    def test_stopped_or_invalid_managed_pid_does_not_read_proc(self):
        manager = ProcessManager()
        for pid in (None, 0, -1, "invalid/pid"):
            with self.subTest(pid=pid), patch.object(manager, "get_pid", return_value=pid):
                with patch("process_mgr.Path.read_text") as read:
                    self.assertIsNone(manager.get_memory("mihomo"))
                    read.assert_not_called()

    def test_missing_permissions_and_malformed_proc_status_are_optional(self):
        manager = ProcessManager()
        with patch.object(manager, "get_pid", return_value=123):
            for error in (FileNotFoundError(), PermissionError()):
                with self.subTest(error=type(error).__name__), patch("process_mgr.Path.read_text", side_effect=error):
                    self.assertIsNone(manager.get_memory("mihomo"))
            for status in ("Name:\tmihomo\n", "VmRSS: -1 kB\n", "VmRSS: 12 MB\n", "VmRSS: unknown kB\n"):
                with self.subTest(status=status), patch("process_mgr.Path.read_text", return_value=status):
                    self.assertIsNone(manager.get_memory("mihomo"))


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "data.json"))
        self.store.update_settings(AppSettings(mihomo_api_secret="private-test-secret"))
        self.manager = Mock(spec=ProcessManager)
        self.manager.running.return_value = True
        self.manager.sing_box_running.return_value = True
        self.manager.get_memory.return_value = None
        router = create_router(self.store, self.manager)
        self.endpoint = next(route.endpoint for route in router.routes if route.path == "/api/dashboard")
        self.real_client = httpx.AsyncClient

    async def collect(self, handler):
        def client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertLessEqual(kwargs["timeout"], 2.0)
            return self.real_client(transport=httpx.MockTransport(handler), **kwargs)

        with patch("api.routes.httpx.AsyncClient", side_effect=client_factory):
            return await self.endpoint()

    async def test_both_cores_are_fetched_concurrently_and_secret_stays_with_mihomo(self):
        requests = []
        active = peak = 0
        both_started = asyncio.Event()

        async def handler(request):
            nonlocal active, peak
            requests.append(request)
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active -= 1
            data = {"downloadTotal": 4096, "uploadTotal": 1024, "connections": [{"id": "one"}]}
            if request.url.port == 9090:
                data["memory"] = 8388608
            return httpx.Response(200, json=data)

        result = await self.collect(handler)
        self.assertEqual(peak, 2)
        self.assertGreater(result["timestamp"], 0)
        self.assertTrue(result["mihomo"]["available"])
        self.assertEqual(result["mihomo"]["download_total"], 4096)
        self.assertEqual(result["mihomo"]["upload_total"], 1024)
        self.assertEqual(result["mihomo"]["memory"], 8388608)
        self.assertEqual(result["mihomo"]["connections"], 1)
        self.assertIsNone(result["mihomo"]["error"])
        self.assertTrue(result["sing_box"]["available"])
        self.assertIsNone(result["sing_box"]["memory"])
        by_port = {request.url.port: request for request in requests if request.url.path == "/connections"}
        self.assertEqual(by_port[9090].headers["authorization"], "Bearer private-test-secret")
        self.assertNotIn("authorization", by_port[9091].headers)
        self.assertTrue(all(request.url.host == "127.0.0.1" for request in requests))


    async def test_resident_memory_takes_precedence_for_both_cores(self):
        self.manager.get_memory.side_effect = lambda core: {"mihomo": 78286848, "sing_box": 19296256}[core]
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"downloadTotal": 100, "uploadTotal": 200, "memory": 10, "connections": []})

        result = await self.collect(handler)
        self.assertEqual(result["mihomo"]["memory"], 78286848)
        self.assertEqual(result["sing_box"]["memory"], 19296256)
        self.assertTrue(result["mihomo"]["available"])
        self.assertEqual(result["mihomo"]["download_total"], 100)
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(request.url.path == "/connections" for request in requests))

    async def test_unavailable_resident_memory_falls_back_only_to_positive_api_value(self):
        for memory in (None, 0, 8388608):
            with self.subTest(memory=memory):
                def handler(request):
                    return httpx.Response(200, json={"downloadTotal": 100, "uploadTotal": 200, "memory": memory, "connections": []})

                result = await self.collect(handler)
                for core in ("mihomo", "sing_box"):
                    self.assertTrue(result[core]["available"])
                    self.assertEqual(result[core]["download_total"], 100)
                    self.assertEqual(result[core]["memory"], memory or None)
                    self.assertIsNone(result[core]["error"])

    async def test_stopped_core_is_not_contacted_and_other_core_still_reports(self):
        self.manager.running.return_value = False
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"downloadTotal": 0, "uploadTotal": 0, "connections": []})

        result = await self.collect(handler)
        self.assertEqual([request.url.port for request in requests], [9091])
        self.assertFalse(result["mihomo"]["available"])
        self.assertIsNone(result["mihomo"]["download_total"])
        self.assertEqual(result["mihomo"]["error"], "核心未运行")
        self.assertTrue(result["sing_box"]["available"])
        self.assertEqual(result["sing_box"]["connections"], 0)

    async def test_core_errors_do_not_hide_healthy_peer(self):
        for failure in ("timeout", "connection", "unauthorized", "invalid_json", "invalid_totals"):
            with self.subTest(failure=failure):
                def handler(request):
                    if request.url.port == 9091:
                        return httpx.Response(200, json={"downloadTotal": 10, "uploadTotal": 20, "connections": []})
                    if failure == "timeout":
                        raise httpx.ReadTimeout("timeout", request=request)
                    if failure == "connection":
                        raise httpx.ConnectError("refused", request=request)
                    if failure == "unauthorized":
                        return httpx.Response(401, json={"message": "unauthorized"})
                    if failure == "invalid_json":
                        return httpx.Response(200, text="invalid json")
                    return httpx.Response(200, json={"downloadTotal": -1, "uploadTotal": True})

                result = await self.collect(handler)
                self.assertFalse(result["mihomo"]["available"])
                self.assertIsNone(result["mihomo"]["download_total"])
                self.assertTrue(result["mihomo"]["error"])
                self.assertTrue(result["sing_box"]["available"])
                self.assertEqual(result["sing_box"]["upload_total"], 20)


class StatusTests(unittest.TestCase):
    def test_status_includes_installed_versions_without_starting_cores(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(str(Path(directory) / "data.json"))
            manager = ProcessManager()
            app = FastAPI()
            app.include_router(create_router(store, manager))
            with patch.object(manager, "get_version", side_effect=["v1.20.0", "1.13.0"]):
                response = TestClient(app).get("/api/status")
            self.assertEqual(response.status_code, 200)
            result = response.json()
            self.assertFalse(result["mihomo_running"])
            self.assertFalse(result["sing_box_running"])
            self.assertIsNone(result["mihomo_pid"])
            self.assertEqual(result["mihomo_version"], "v1.20.0")
            self.assertEqual(result["sing_box_version"], "1.13.0")


if __name__ == "__main__":
    unittest.main()
