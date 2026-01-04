"""Settings load/save helpers."""

from __future__ import annotations

import json
import os

from .app_config import altheapath, settings_path


DEFAULT_SETTINGS = {
    # "window_and_tray" (default): open main window + tray indicator
    # "tray_only": start in tray (no main window)
    "startup_mode": "window_and_tray",
}


def load_settings() -> dict:
    try:
        path = settings_path()
        if not os.path.exists(path):
            return dict(DEFAULT_SETTINGS)
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            settings.update(raw)
        return settings
    except Exception:
        return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    os.makedirs(altheapath, exist_ok=True)
    path = settings_path()
    payload = dict(DEFAULT_SETTINGS)
    if isinstance(settings, dict):
        payload.update(settings)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp_path, path)
