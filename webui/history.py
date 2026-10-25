"""Past results on disk: saved reports, and the memory log of decisions and their outcomes."""

import json
import re
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt

from tradingagents.default_config import build_default_config
from tradingagents.memory.log import TradingMemoryLog

_md = MarkdownIt("commonmark", {"html": False}).enable("table")

# A report folder as TradingAgentsGraph.default_report_path names it.
_REPORT_DIR = re.compile(r"^(?P<ticker>.+)_(?P<stamp>\d{8}_\d{6})$")


def markdown(text) -> str:
    """Markdown as HTML, raw HTML in it escaped."""
    if not text:
        return ""
    if isinstance(text, list):
        text = "\n".join(str(item) for item in text)
    return _md.render(str(text))


def reports_root(config: dict | None = None) -> Path:
    return Path((config or build_default_config())["results_dir"]) / "reports"


def saved_reports(config: dict | None = None) -> list[dict]:
    """Every saved report folder, newest first."""
    root = reports_root(config)
    if not root.is_dir():
        return []
    found = []
    for folder in root.iterdir():
        match = _REPORT_DIR.match(folder.name)
        if not folder.is_dir() or not match:
            continue
        try:
            saved = datetime.strptime(match["stamp"], "%Y%m%d_%H%M%S").isoformat()
        except ValueError:
            continue
        found.append({
            "name": folder.name,
            "ticker": match["ticker"],
            "saved_at": saved,
            "html": (folder / "complete_report.html").is_file(),
            "markdown": (folder / "complete_report.md").is_file(),
            "rating": _rating_of(folder),
        })
    return sorted(found, key=lambda r: r["saved_at"], reverse=True)


def _rating_of(folder: Path) -> str | None:
    from tradingagents.agents.rating import extract_rating

    decision = folder / "5_portfolio" / "decision.md"
    try:
        return extract_rating(decision.read_text(encoding="utf-8"))
    except OSError:
        return None


def report_file(name: str, filename: str, config: dict | None = None) -> Path:
    """A file inside a saved report folder; anything outside the folder raises ValueError."""
    root = reports_root(config).resolve()
    path = (root / name / filename).resolve()
    if root not in path.parents or not _REPORT_DIR.match(name):
        raise ValueError("not a saved report")
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


def saved_state(report_dir: str | None) -> dict:
    """The state a finished UI run saved next to its report."""
    if not report_dir:
        return {}
    try:
        return json.loads((Path(report_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def decisions(config: dict | None = None) -> list[dict]:
    """The memory log's entries, newest first: rating, outcome once settled, and lesson."""
    entries = TradingMemoryLog(config or build_default_config()).load_entries()
    shown = []
    for entry in entries:
        shown.append({
            "date": entry["date"],
            "ticker": entry["ticker"],
            "rating": entry["rating"],
            "pending": entry["pending"],
            "return": entry.get("raw"),
            "alpha": entry.get("alpha"),
            "holding": entry.get("holding"),
            "resolved": entry.get("resolved"),
            "decision_html": markdown(entry.get("decision")),
            "reflection": entry.get("reflection") or "",
        })
    return sorted(shown, key=lambda e: (e["date"], e["ticker"]), reverse=True)
