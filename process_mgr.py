"""Mihomo process manager — mirrors src/core/process_mgr.cpp."""
from __future__ import annotations

import collections
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
