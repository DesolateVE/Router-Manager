"""Mihomo process manager — mirrors src/core/process_mgr.cpp."""
from __future__ import annotations

import collections
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from config_gen import generate
import singbox_gen
from models import AppState

# Ring buffer shared between relay thread and API
_log_buffer: collections.deque[str] = collections.deque(maxlen=500)
_log_lock = threading.Lock()
_sing_box_log_buffer: collections.deque[str] = collections.deque(maxlen=500)
_sing_box_log_lock = threading.Lock()


def _relay_output(proc: subprocess.Popen) -> None:
    """Read mihomo stdout line-by-line, store in ring buffer, and echo to Python stdout."""
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            with _log_lock:
                _log_buffer.append(line)
            try:
                sys.stdout.write("[mihomo] " + line + "\n")
                sys.stdout.flush()
            except Exception:
                pass
    except Exception:
        pass


def _relay_sing_box_output(proc: subprocess.Popen) -> None:
    try:
        for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            with _sing_box_log_lock:
                _sing_box_log_buffer.append(line)
            try:
                sys.stdout.write("[sing-box] " + line + "\n")
                sys.stdout.flush()
            except Exception:
                pass
    except Exception:
        pass


class ProcessManager:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._sing_box_proc: subprocess.Popen | None = None
        self.last_error: str = ""
        self.sing_box_last_error: str = ""
        self._version_cache: dict[str, tuple[tuple, float, str | None]] = {}
        self._version_lock = threading.Lock()

    def get_version(self, core: str, binary: str) -> str | None:
        """Read the installed binary's version, including while its core is stopped."""
        if core not in ("mihomo", "sing_box"):
            raise ValueError(f"Unknown core: {core}")
        executable = shutil.which(binary) or binary
        try:
            stat = Path(executable).stat()
            signature = (executable, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except OSError:
            signature = (executable, None)
        # A stat change invalidates the cache immediately after a binary upgrade.
        with self._version_lock:
            cached = self._version_cache.get(core)
            if cached and cached[0] == signature and time.monotonic() < cached[1]:
                return cached[2]
            version = None
            try:
                result = subprocess.run(
                    [executable, "-v" if core == "mihomo" else "version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    timeout=2.0,
                    check=True,
                )
                pattern = r"^\s*mihomo(?:\s+meta)?\s+(\S+)" if core == "mihomo" else r"^\s*sing-box\s+version\s+(\S+)"
                match = re.search(pattern, result.stdout, re.IGNORECASE | re.MULTILINE)
                if match:
                    version = match.group(1)
            except (OSError, subprocess.SubprocessError):
                pass
            # Briefly cache failures so a missing or broken binary cannot be spawned on every poll.
            self._version_cache[core] = (signature, time.monotonic() + (300 if version else 30), version)
            return version

    def get_memory(self, core: str) -> int | None:
        """Return a managed core's resident memory in bytes on Linux."""
        if core == "mihomo":
            pid = self.get_pid()
        elif core == "sing_box":
            pid = self.get_sing_box_pid()
        else:
            raise ValueError(f"Unknown core: {core}")
        if pid is None:
            return None
        try:
            pid = int(pid)
            if pid <= 0:
                return None
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace")
            match = re.search(r"^VmRSS:\s+(\d+)\s+kB\s*$", status, re.MULTILINE)
            return int(match.group(1)) * 1024 if match else None
        except (OSError, TypeError, ValueError):
            return None

    def start(self, state: AppState) -> bool:
        if self.running():
            self.stop()

        # Write config to file
        yaml_text = generate(state)
        config_path = Path(state.settings.data_dir) / "config.yaml"
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(yaml_text, encoding="utf-8")
        except OSError as e:
            self.last_error = f"Failed to write config: {e}"
            return False

        try:
            self._proc = subprocess.Popen(
                [
                    state.settings.mihomo_bin,
                    "-f",
                    str(config_path),
                    "-d",
                    state.settings.data_dir,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            self.last_error = f"Failed to launch mihomo binary: {e}"
            self._proc = None
            return False

        # Wait up to 500 ms to detect an immediate crash
        for _ in range(10):
            time.sleep(0.05)
            if self._proc.poll() is not None:
                # Process already exited — read its output for diagnostics
                try:
                    out = self._proc.stdout.read().decode(errors="replace").strip()
                except Exception:
                    out = ""
                self.last_error = out or f"mihomo exited with code {self._proc.returncode}"
                self._proc = None
                return False

        self.last_error = ""
        t = threading.Thread(target=_relay_output, args=(self._proc,), daemon=True)
        t.start()
        return True

    def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.send_signal(signal.SIGTERM)
            # Wait up to 3 seconds for graceful shutdown
            for _ in range(30):
                if self._proc.poll() is not None:
                    self._proc = None
                    return
                time.sleep(0.1)
            self._proc.kill()
            self._proc.wait()
        except (OSError, ProcessLookupError):
            pass
        finally:
            self._proc = None

    def restart(self, state: AppState) -> bool:
        self.stop()
        return self.start(state)

    def running(self) -> bool:
        if self._proc is None:
            return False
        return self._proc.poll() is None

    def get_pid(self) -> int | None:
        if self._proc and self._proc.poll() is None:
            return self._proc.pid
        return None

    def get_logs(self, n: int = 200) -> list[str]:
        with _log_lock:
            lines = list(_log_buffer)
        return lines[-n:] if n < len(lines) else lines

    def clear_logs(self) -> None:
        with _log_lock:
            _log_buffer.clear()

    def start_sing_box(self, state: AppState) -> bool:
        if self.sing_box_running():
            self.stop_sing_box()

        config_text = singbox_gen.generate(state)
        config_path = Path(state.settings.data_dir) / "sing-box.json"
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(config_text, encoding="utf-8")
        except OSError as e:
            self.sing_box_last_error = f"Failed to write sing-box config: {e}"
            return False

        try:
            self._sing_box_proc = subprocess.Popen(
                [state.settings.sing_box_bin, "run", "-c", str(config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as e:
            self.sing_box_last_error = f"Failed to launch sing-box binary: {e}"
            self._sing_box_proc = None
            return False

        for _ in range(10):
            time.sleep(0.05)
            if self._sing_box_proc.poll() is not None:
                try:
                    out = self._sing_box_proc.stdout.read().decode(errors="replace").strip()
                except Exception:
                    out = ""
                self.sing_box_last_error = out or f"sing-box exited with code {self._sing_box_proc.returncode}"
                self._sing_box_proc = None
                return False

        self.sing_box_last_error = ""
        t = threading.Thread(target=_relay_sing_box_output, args=(self._sing_box_proc,), daemon=True)
        t.start()
        return True

    def stop_sing_box(self) -> None:
        if self._sing_box_proc is None:
            return
        try:
            self._sing_box_proc.send_signal(signal.SIGTERM)
            for _ in range(30):
                if self._sing_box_proc.poll() is not None:
                    self._sing_box_proc = None
                    return
                time.sleep(0.1)
            self._sing_box_proc.kill()
            self._sing_box_proc.wait()
        except (OSError, ProcessLookupError):
            pass
        finally:
            self._sing_box_proc = None

    def restart_sing_box(self, state: AppState) -> bool:
        self.stop_sing_box()
        return self.start_sing_box(state)

    def sing_box_running(self) -> bool:
        if self._sing_box_proc is None:
            return False
        return self._sing_box_proc.poll() is None

    def get_sing_box_pid(self) -> int | None:
        if self._sing_box_proc and self._sing_box_proc.poll() is None:
            return self._sing_box_proc.pid
        return None

    def get_sing_box_logs(self, n: int = 200) -> list[str]:
        with _sing_box_log_lock:
            lines = list(_sing_box_log_buffer)
        return lines[-n:] if n < len(lines) else lines

    def clear_sing_box_logs(self) -> None:
        with _sing_box_log_lock:
            _sing_box_log_buffer.clear()
