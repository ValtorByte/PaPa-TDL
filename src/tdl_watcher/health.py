"""Recent, verified account connectivity for the terminal dashboard."""

import json
import time

from .config import private_json


def record(settings, provider: str) -> None:
    if provider not in {"telegram", "drive"}:
        raise ValueError("Unknown account provider.")
    path = settings.data_dir / "connection-health.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    state[provider] = time.time()
    private_json(path, json.dumps(state, separators=(",", ":")))


def verified_recently(settings, provider: str, *, max_age: int = 120) -> bool:
    try:
        state = json.loads((settings.data_dir / "connection-health.json").read_text(encoding="utf-8"))
        stamp = float(state[provider])
        return 0 <= time.time() - stamp <= max_age
    except (OSError, ValueError, TypeError, KeyError):
        return False
