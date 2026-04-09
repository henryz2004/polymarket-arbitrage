"""
Config helpers for watchdog app CLIs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_cli_defaults(config_path: str) -> dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        return {}
    with path.open("r") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return data
