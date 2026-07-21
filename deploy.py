#!/usr/bin/env python3
"""
Deploy Router Manager to OpenWrt via SSH.

Examples:
  python deploy.py --host 10.0.8.84 --user root
  python deploy.py --ip 10.0.8.84 --username root --password weiyi
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


DEFAULT_HOST = "10.0.8.84"
DEFAULT_USER = "root"
SERVICE_NAME = "router_manager"
LEGACY_SERVICE_NAME = "mihomo_helper"
DEFAULT_TARGET_DIR = "/opt/router_manager"
DEFAULT_DATA_DIR = "/etc/router_manager"
LEGACY_TARGET_DIR = "/opt/mihomo_helper"
LEGACY_DATA_DIR = "/etc/mihomo_helper"
DEFAULT_SERVICE_PORT = "8080"
METACUBEXD_ARCHIVE_NAME = "metacubexd-gh-pages.zip"

ROOT = Path(__file__).resolve().parent
INIT_TEMPLATE = ROOT / "router_manager.init"
BUILD_DIR = ROOT / ".deploy"
BUILD_INIT_SCRIPT = BUILD_DIR / SERVICE_NAME


def log(message: str) -> None:
    print(f"[deploy] {message}", flush=True)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def require_command(command: str) -> None:
    if shutil.which(command) is None:
        raise SystemExit(f"Error: required command not found: {command}")


class SubprocessSshClient:
    """SSH client for key/agent based auth through local ssh/scp."""

    def __init__(self, host: str, user: str, connect_timeout: int) -> None:
        self.host = host
        self.user = user
        self.ssh_target = f"{user}@{host}"
        self.ssh_base = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={connect_timeout}",
        ]
        self.scp_base = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={connect_timeout}",
        ]

    def run(self, command: List[str], input_text: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(command, cwd=ROOT, input=input_text, text=True, check=True)

    def ssh(self, command: str) -> None:
        self.run([*self.ssh_base, self.ssh_target, command])

    def ssh_capture(self, command: str) -> str:
        result = subprocess.run(
            [*self.ssh_base, self.ssh_target, command],
            cwd=ROOT,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def upload_files(self, sources: List[Path], remote_dir: str) -> None:
        self.run([*self.scp_base, *(str(source) for source in sources), f"{self.ssh_target}:{remote_dir}/"])

    def upload_file_to_path(self, source: Path, remote_path: str) -> None:
        self.run([*self.scp_base, str(source), f"{self.ssh_target}:{remote_path}"])

    def close(self) -> None:
        pass


class ParamikoSshClient:
    """SSH client for password auth without sshpass."""

    def __init__(self, host: str, user: str, password: str, connect_timeout: int) -> None:
        try:
            import paramiko
        except ImportError as exc:
            raise SystemExit("Error: password login requires paramiko. Install it with: pip install paramiko") from exc

        self.host = host
        self.user = user
        self.paramiko = paramiko
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=host,
            username=user,
            password=password,
            timeout=connect_timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        self.sftp = self.client.open_sftp()

    def ssh(self, command: str) -> None:
        self._exec(command)

    def ssh_capture(self, command: str) -> str:
        return self._exec(command, print_output=False).strip()

    def upload_files(self, sources: List[Path], remote_dir: str) -> None:
        for source in sources:
            self.sftp.put(str(source), f"{remote_dir.rstrip('/')}/{source.name}")

    def upload_file_to_path(self, source: Path, remote_path: str) -> None:
        self.sftp.put(str(source), remote_path)

    def close(self) -> None:
        self.sftp.close()
        self.client.close()

    def _exec(self, command: str, print_output: bool = True) -> str:
        stdin, stdout, stderr = self.client.exec_command(command)
        stdin.close()
        output = stdout.read().decode(errors="replace")
        error = stderr.read().decode(errors="replace")
        status = stdout.channel.recv_exit_status()
        if status != 0:
            if output:
                print(output, end="")
            if error:
                print(error, end="", file=sys.stderr)
            raise subprocess.CalledProcessError(status, command)
        if print_output and output:
            print(output, end="")
        if print_output and error:
            print(error, end="", file=sys.stderr)
        return output


class Deployer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.host = args.host
        self.user = args.user
        self.target_dir = args.target_dir
        self.data_dir = args.data_dir
        self.service_port = str(args.port)

        if args.password:
            self.client = ParamikoSshClient(args.host, args.user, args.password, args.connect_timeout)
        else:
            require_command("ssh")
            require_command("scp")
            self.client = SubprocessSshClient(args.host, args.user, args.connect_timeout)

    def deploy(self) -> None:
        try:
            self._deploy()
        finally:
            self.client.close()

    def _deploy(self) -> None:
        log(f"Connecting to {self.user}@{self.host} ...")
        self.client.ssh("uname -a")

        self._ensure_target_packages()

        log("Stopping existing services before update ...")
        self.client.ssh(
            f"if [ -x /etc/init.d/{shell_quote(SERVICE_NAME)[1:-1]} ]; then /etc/init.d/{SERVICE_NAME} stop || true; fi; "
            f"if [ -x /etc/init.d/{LEGACY_SERVICE_NAME} ]; then /etc/init.d/{LEGACY_SERVICE_NAME} stop || true; fi"
        )

        log(f"Creating remote directories: {self.target_dir}  {self.data_dir}")
        self.client.ssh(
            "mkdir -p "
            f"{shell_quote(self.target_dir + '/api')} "
            f"{shell_quote(self.target_dir + '/web')} "
            f"{shell_quote(self.data_dir)}"
        )

        self._migrate_legacy_data_dir()

        log("Uploading Python source files ...")
        self.client.upload_files(
            [
                ROOT / "main.py",
                ROOT / "models.py",
                ROOT / "config_gen.py",
                ROOT / "singbox_gen.py",
                ROOT / "import_engine.py",
                ROOT / "process_mgr.py",
                ROOT / "store.py",
                ROOT / "requirements.txt",
            ],
            self.target_dir,
        )

        log("Uploading api/ ...")
        self.client.upload_files([ROOT / "api" / "__init__.py", ROOT / "api" / "routes.py"], f"{self.target_dir}/api")

        log("Uploading web/ ...")
        self.client.upload_files(
            [ROOT / "web" / "index.html", ROOT / "web" / "app.js", ROOT / "web" / "style.css"],
            f"{self.target_dir}/web",
        )

        log(f"Uploading resource files to {self.data_dir} ...")
        for source in sorted((ROOT / "resource").iterdir()):
            if source.is_file():
                log(f"  -> {source.relative_to(ROOT).as_posix()}")
                self.client.upload_files([source], self.data_dir)

        self._extract_metacubexd_archive()

        log("Installing Python dependencies on target ...")
        self.client.ssh(
            "python3 -m pip install --no-input -r "
            f"{shell_quote(self.target_dir + '/requirements.txt')}"
        )

        log("Setting execute permission on resource executables ...")
        self.client.ssh(
            "chmod +x "
            f"{shell_quote(self.data_dir + '/mihomo_firewall.sh')} "
            f"{shell_quote(self.data_dir + '/mihomo_cleanup.sh')} "
            f"{shell_quote(self.data_dir + '/mihomo')} "
            "2>/dev/null; true"
        )

        log(f"Writing /etc/init.d/{SERVICE_NAME} ...")
        init_script = self.render_init_script()
        self.client.upload_file_to_path(init_script, f"/etc/init.d/{SERVICE_NAME}")
        self.client.ssh(f"chmod +x /etc/init.d/{SERVICE_NAME}")

        log("Disabling legacy service name if present ...")
        self.client.ssh(
            f"if [ -x /etc/init.d/{LEGACY_SERVICE_NAME} ]; then "
            f"/etc/init.d/{LEGACY_SERVICE_NAME} disable || true; rm -f /etc/init.d/{LEGACY_SERVICE_NAME}; fi"
        )

        log("Enabling service (auto-start on boot) ...")
        self.client.ssh(f"/etc/init.d/{SERVICE_NAME} enable")

        log("Starting service ...")
        self.client.ssh(f"/etc/init.d/{SERVICE_NAME} start")

        time.sleep(2)
        try:
            status = self.client.ssh_capture(f"/etc/init.d/{SERVICE_NAME} status")
        except subprocess.CalledProcessError:
            status = "unknown"
        log(f"Service status: {status}")

        log(f"Done! Web UI:  http://{self.host}:{self.service_port}")

    def _ensure_target_packages(self) -> None:
        log("Checking target runtime packages ...")
        self.client.ssh(
            "set -e; "
            "if ! command -v apk >/dev/null 2>&1; then "
            "echo 'Error: apk package manager not found on target. This deploy script expects apk-based OpenWrt.' >&2; "
            "exit 1; "
            "fi; "
            "missing=''; "
            "if ! command -v python3 >/dev/null 2>&1; then missing=\"$missing python3\"; fi; "
            "if ! python3 -m pip --version >/dev/null 2>&1; then missing=\"$missing py3-pip\"; fi; "
            "if ! command -v sing-box >/dev/null 2>&1; then missing=\"$missing sing-box\"; fi; "
            "if [ -n \"$missing\" ]; then "
            "echo \"Installing missing packages:$missing\"; "
            "apk update; "
            "apk add $missing; "
            "fi; "
            "python3 --version; "
            "python3 -m pip --version; "
            "sing-box version"
        )

    def _migrate_legacy_data_dir(self) -> None:
        if self.data_dir == LEGACY_DATA_DIR:
            return
        log("Migrating legacy data directory if needed ...")
        self.client.ssh(
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "import json\n"
            f"legacy = Path({LEGACY_DATA_DIR!r})\n"
            f"target = Path({self.data_dir!r})\n"
            "target.mkdir(parents=True, exist_ok=True)\n"
            "legacy_data = legacy / 'data.json'\n"
            "target_data = target / 'data.json'\n"
            "if legacy_data.exists() and not target_data.exists():\n"
            "    data = json.loads(legacy_data.read_text(encoding='utf-8'))\n"
            "    settings = data.get('settings', {})\n"
            f"    if settings.get('data_dir') == {LEGACY_DATA_DIR!r}:\n"
            f"        settings['data_dir'] = {self.data_dir!r}\n"
            f"    if settings.get('mihomo_bin') == {LEGACY_DATA_DIR + '/mihomo'!r}:\n"
            f"        settings['mihomo_bin'] = {self.data_dir + '/mihomo'!r}\n"
            "    data['settings'] = settings\n"
            "    target_data.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')\n"
            "PY"
        )

    def _extract_metacubexd_archive(self) -> None:
        log(f"Extracting {METACUBEXD_ARCHIVE_NAME} in {self.data_dir} ...")
        remote_archive = self.data_dir.rstrip("/") + "/" + METACUBEXD_ARCHIVE_NAME
        self.client.ssh(
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "from zipfile import ZipFile\n"
            f"archive = Path({remote_archive!r})\n"
            f"target = Path({self.data_dir!r})\n"
            "if archive.exists():\n"
            "    target.mkdir(parents=True, exist_ok=True)\n"
            "    with ZipFile(archive) as zf:\n"
            "        for member in zf.infolist():\n"
            "            dest = (target / member.filename).resolve()\n"
            "            if target.resolve() not in dest.parents and dest != target.resolve():\n"
            "                raise RuntimeError(f'Unsafe zip entry: {member.filename}')\n"
            "        zf.extractall(target)\n"
            "PY"
        )

    def render_init_script(self) -> Path:
        template = INIT_TEMPLATE.read_text(encoding="utf-8")
        content = (
            template.replace("__TARGET_DIR__", self.target_dir)
            .replace("__DATA_DIR__", self.data_dir)
            .replace("__SERVICE_PORT__", self.service_port)
        )
        BUILD_DIR.mkdir(exist_ok=True)
        BUILD_INIT_SCRIPT.write_text(content, encoding="utf-8", newline="\n")
        os.chmod(BUILD_INIT_SCRIPT, 0o755)
        return BUILD_INIT_SCRIPT


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy Router Manager to OpenWrt via SSH.")
    parser.add_argument("--host", "--ip", dest="host", default=DEFAULT_HOST, help=f"OpenWrt IP/host, default: {DEFAULT_HOST}")
    parser.add_argument("--user", "--username", dest="user", default=DEFAULT_USER, help=f"SSH username, default: {DEFAULT_USER}")
    parser.add_argument("--password", "-p", help="SSH password. If omitted, SSH uses your local key/agent.")
    parser.add_argument("--target-dir", default=DEFAULT_TARGET_DIR, help=f"Remote app directory, default: {DEFAULT_TARGET_DIR}")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help=f"Remote data directory, default: {DEFAULT_DATA_DIR}")
    parser.add_argument("--port", default=DEFAULT_SERVICE_PORT, help=f"Web UI port, default: {DEFAULT_SERVICE_PORT}")
    parser.add_argument("--connect-timeout", type=int, default=10, help="SSH connect timeout in seconds, default: 10")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        Deployer(args).deploy()
    except subprocess.CalledProcessError as exc:
        command = " ".join(str(part) for part in exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
        print(f"Error: command failed with exit code {exc.returncode}: {command}", file=sys.stderr)
        return exc.returncode
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
