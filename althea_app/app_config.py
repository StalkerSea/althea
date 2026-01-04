"""Paths and environment-derived configuration.

This module centralizes all filesystem locations and bundled resource lookup.
"""

from __future__ import annotations

import os
import platform


computer_cpu_platform = platform.machine()


def _project_root() -> str:
    # This file lives in <root>/althea_app/app_config.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_installed() -> bool:
    system_base = "/usr/lib/althea"
    return os.path.exists(os.path.join(system_base, "althea"))


def resource_path(relative_path: str) -> str:
    """Return an absolute path for bundled resources."""
    system_base = "/usr/lib/althea"
    if os.path.exists(os.path.join(system_base, "althea")):
        base_path = system_base
    else:
        base_path = _project_root()
    return os.path.join(base_path, relative_path)


altheapath = os.path.join(
    os.environ.get("XDG_DATA_HOME") or f"{os.environ['HOME']}/.local/share",
    "althea",
)

AltServer = os.path.join(altheapath, "AltServer")
AnisetteServer = os.path.join(altheapath, "anisette-server")
Netmuxd = os.path.join(altheapath, "netmuxd")
AltStore = os.path.join(altheapath, "AltStore.ipa")

AutoStart = resource_path("resources/AutoStart.sh")


def settings_path() -> str:
    return os.path.join(altheapath, "config.json")


def log_path() -> str:
    return os.path.join(altheapath, "althea.log")
