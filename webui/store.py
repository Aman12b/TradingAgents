"""Small JSON files under one folder, written whole and atomically."""

import json
import os
import threading
from pathlib import Path

from tradingagents.default_config import _TRADINGAGENTS_HOME

DEFAULT_DIR = Path(os.environ.get("TRADINGAGENTS_WEBUI_DIR") or Path(_TRADINGAGENTS_HOME) / "webui")


class JsonStore:
    """One JSON document per name in ``folder``; reads fall back to a default."""

    def __init__(self, folder: Path | str = DEFAULT_DIR):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load(self, name: str, default):
        path = self.folder / f"{name}.json"
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return default

    def save(self, name: str, value) -> None:
        path = self.folder / f"{name}.json"
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(json.dumps(value, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
