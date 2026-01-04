"""iOS device discovery helpers."""

from __future__ import annotations

import os
import subprocess

from .logging_utils import log_info


def get_connected_device() -> dict:
    """Return {udid, transport} where transport is 'usb' | 'network' | 'none'."""
    # Prefer USB via default usbmuxd.
    try:
        log_info("get_connected_device: running idevice_id -l")
        out = subprocess.check_output(["idevice_id", "-l"], stderr=subprocess.STDOUT)
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_connected_device: usb udids={udids}")
            return {"udid": udids[0], "transport": "usb"}
    except subprocess.CalledProcessError as e:
        log_info(f"get_connected_device: idevice_id -l failed rc={e.returncode}")
    except Exception as e:
        log_info(f"get_connected_device: idevice_id -l failed: {e!r}")

    # Fallback to network via netmuxd.
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("get_connected_device: running idevice_id -n -l (via netmuxd)")
        out = subprocess.check_output(
            ["idevice_id", "-n", "-l"], env=env, stderr=subprocess.STDOUT
        )
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_connected_device: network udids={udids}")
            return {"udid": udids[0], "transport": "network"}
    except subprocess.CalledProcessError as e:
        log_info(f"get_connected_device: idevice_id -n -l failed rc={e.returncode}")
    except Exception as e:
        log_info(f"get_connected_device: idevice_id -n -l failed: {e!r}")

    return {"udid": "", "transport": "none"}


def get_connected_udid() -> str:
    """Return a single device UDID, preferring USB and falling back to network."""
    return get_connected_device().get("udid", "")


def get_network_udid() -> str:
    """Return a device UDID specifically from network connection."""
    try:
        env = os.environ.copy()
        env["USBMUXD_SOCKET_ADDRESS"] = "127.0.0.1:27015"
        log_info("get_network_udid: running idevice_id -n -l")
        out = subprocess.check_output(
            ["idevice_id", "-n", "-l"], env=env, stderr=subprocess.STDOUT
        )
        udids = [
            line.strip()
            for line in out.decode(errors="replace").splitlines()
            if line.strip()
        ]
        if udids:
            log_info(f"get_network_udid: candidates={udids}")
            return udids[0]
    except subprocess.CalledProcessError:
        pass

    return ""
