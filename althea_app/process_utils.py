"""Process helpers."""

from __future__ import annotations

import psutil


def _safe_cmdline_str(proc_info: dict) -> str:
    cmdline = proc_info.get("cmdline")
    if not cmdline:
        return ""
    if isinstance(cmdline, str):
        return cmdline
    try:
        return " ".join(cmdline)
    except TypeError:
        return ""


def is_process_running(process_name: str, *, ignore_pid: int | None = None) -> bool:
    """Check if a process containing `process_name` in its cmdline is running."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if ignore_pid is not None and proc.info.get("pid") == ignore_pid:
                continue
            cmdline_str = _safe_cmdline_str(proc.info)
            if process_name in cmdline_str:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return False


def kill_process_by_name(process_name: str) -> None:
    """Kill all processes with the given name."""
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline_str = _safe_cmdline_str(proc.info)
            if process_name in cmdline_str:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
