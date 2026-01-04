"""Service process management (AltServer, anisette-server, netmuxd)."""

from __future__ import annotations

import os
import subprocess
import urllib.request

from .app_config import AltServer, AnisetteServer, Netmuxd, altheapath
from .logging_utils import log_info
from .process_utils import is_process_running, kill_process_by_name


def stop_services() -> None:
    for needle in (AltServer, AnisetteServer, Netmuxd):
        try:
            kill_process_by_name(needle)
        except Exception:
            pass


def is_anisette_accessible(timeout: float = 0.75) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:6969", timeout=timeout) as resp:
            data = resp.read(2048)
        return b"{" in data
    except Exception:
        return False


def is_netmuxd_ready(timeout: float = 0.5) -> bool:
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        proc = subprocess.run(
            ["idevice_id", "-n", "-l"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def is_altserver_running() -> bool:
    # Match by full path placed in cmdline.
    return is_process_running(AltServer)


def start_anisette_server() -> None:
    if is_anisette_accessible(timeout=0.5):
        return
    try:
        kill_process_by_name(AnisetteServer)
    except Exception:
        pass
    log_info("Starting anisette-server")
    subprocess.Popen(
        [AnisetteServer, "-n", "127.0.0.1", "-p", "6969"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_netmuxd() -> None:
    if is_netmuxd_ready(timeout=0.25):
        return
    try:
        kill_process_by_name(Netmuxd)
    except Exception:
        pass
    log_info("Starting netmuxd")
    subprocess.Popen(
        [Netmuxd, "--disable-unix", "--host", "127.0.0.1"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_altserver() -> None:
    if is_altserver_running():
        return
    log_info("Starting AltServer")
    env = os.environ.copy()
    env["ALTSERVER_ANISETTE_SERVER"] = "http://127.0.0.1:6969"
    env["AVAHI_COMPAT_NOWARN"] = "1"

    # If we force USBMUXD_SOCKET_ADDRESS to netmuxd, AltServer may not see USB devices.
    # Prefer default usbmuxd when any USB device is present; otherwise use netmuxd.
    try:
        out = subprocess.check_output(["idevice_id", "-l"], stderr=subprocess.DEVNULL)
        has_usb = any(line.strip() for line in out.decode(errors="replace").splitlines())
    except Exception:
        has_usb = False

    if has_usb:
        env.pop("USBMUXD_SOCKET_ADDRESS", None)
        log_info("AltServer env: using default usbmuxd socket (USB present)")
    else:
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("AltServer env: using netmuxd socket (no USB devices)")

    subprocess.Popen(
        [os.path.join(altheapath, "AltServer")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def restart_anisette_server() -> None:
    log_info("Restart anisette-server requested")
    start_anisette_server()


def restart_netmuxd() -> None:
    log_info("Restart netmuxd requested")
    start_netmuxd()


def restart_altserver_process() -> None:
    log_info("Restart AltServer requested")
    try:
        kill_process_by_name(AltServer)
    except Exception:
        pass
    start_altserver()


def _is_usbmuxd_responsive(timeout_s: float = 2.0) -> bool:
    """Best-effort probe that usbmuxd is responding."""
    try:
        proc = subprocess.run(
            ["idevice_id", "-l"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def restart_lockdownd_service() -> None:
    """Restart host-side services involved in lockdownd communication."""
    log_info("Restart lockdownd requested")

    units = ["lockdownd", "usbmuxd"]
    last_out = ""
    for unit in units:
        try:
            proc = subprocess.run(
                ["systemctl", "restart", unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=6,
                check=False,
            )
            out = (proc.stdout or b"").decode(errors="replace")
            last_out = out.strip()
            if proc.returncode == 0:
                log_info(f"Restart lockdownd: systemctl restart {unit} succeeded")
                return
            log_info(
                f"Restart lockdownd: systemctl restart {unit} failed rc={proc.returncode} output_tail={last_out[-250:]!r}"
            )
        except FileNotFoundError:
            raise RuntimeError("systemctl not found on this system")
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out restarting {unit}")

    if last_out:
        raise RuntimeError(last_out)
    raise RuntimeError("Failed to restart lockdownd/usbmuxd")
