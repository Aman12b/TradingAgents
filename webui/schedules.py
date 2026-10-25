"""Recurring runs: a watchlist analysed at set times on set weekdays.

A schedule fires while the server is up. A start that falls while it is down is
skipped, not made up later, so a restart never queues a burst of paid runs.
"""

import datetime
import logging
import threading
import uuid

from .runner import make_job, now_iso
from .settings import clean_settings
from .store import JsonStore

logger = logging.getLogger(__name__)

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_time(value: str) -> datetime.time:
    try:
        return datetime.datetime.strptime(value.strip(), "%H:%M").time()
    except (AttributeError, ValueError):
        raise ValueError(f"not a time: {value!r}; use HH:MM") from None


def next_run(schedule: dict, after: datetime.datetime) -> datetime.datetime | None:
    """The first start of ``schedule`` strictly after ``after``, or None when it has none."""
    days = sorted(set(schedule.get("weekdays") or []))
    times = sorted(parse_time(t) for t in schedule.get("times") or [])
    if not days or not times:
        return None
    for offset in range(8):
        day = after.date() + datetime.timedelta(days=offset)
        if day.weekday() not in days:
            continue
        for t in times:
            start = datetime.datetime.combine(day, t)
            if start > after:
                return start
    return None


def analysis_date_for(schedule: dict, start: datetime.datetime) -> str:
    """The analysis date a start uses: its own day, or the weekday before it."""
    day = start.date()
    if schedule.get("date_mode") == "previous_day":
        day -= datetime.timedelta(days=1)
        while day.weekday() >= 5:
            day -= datetime.timedelta(days=1)
    return day.isoformat()


def clean_schedule(values: dict, defaults: dict, existing: dict | None = None) -> dict:
    """A schedule from the form's ``values``; a bad field raises ValueError."""
    tickers = [t.strip() for t in str(values.get("tickers") or "").replace(";", ",").split(",") if t.strip()]
    if not tickers:
        raise ValueError("Name at least one ticker.")
    times = [t.strip() for t in str(values.get("times") or "").split(",") if t.strip()]
    if not times:
        raise ValueError("Name at least one time, e.g. 22:00.")
    times = [parse_time(t).strftime("%H:%M") for t in times]
    weekdays = sorted({int(d) for d in values.get("weekdays") or [] if 0 <= int(d) <= 6})
    if not weekdays:
        raise ValueError("Pick at least one weekday.")
    schedule = dict(existing or {})
    schedule.update({
        "id": (existing or {}).get("id") or uuid.uuid4().hex[:10],
        "name": str(values.get("name") or ", ".join(tickers)).strip(),
        "tickers": tickers,
        "times": times,
        "weekdays": weekdays,
        "date_mode": "previous_day" if values.get("date_mode") == "previous_day" else "same_day",
        "enabled": bool(values.get("enabled", True)),
        # None follows the settings page; a dict pins this schedule's own.
        "settings": clean_settings(values["settings"], defaults) if values.get("settings") else None,
    })
    schedule.setdefault("created_at", now_iso())
    schedule.setdefault("last_run_at", None)
    schedule.setdefault("history", [])
    return schedule


class Scheduler:
    """Keeps the schedules and queues their jobs when a start comes due."""

    def __init__(self, store: JsonStore, jobs, defaults):
        self.store = store
        self.jobs = jobs
        self.defaults = defaults        # callable: the settings page's current settings
        self.schedules: dict[str, dict] = {s["id"]: s for s in store.load("schedules", [])}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        now = datetime.datetime.now()
        for schedule in self.schedules.values():
            self._plan(schedule, now)
        self._save()

    def _save(self):
        self.store.save("schedules", list(self.schedules.values()))

    def _plan(self, schedule: dict, after: datetime.datetime):
        start = next_run(schedule, after) if schedule.get("enabled") else None
        schedule["next_run_at"] = start.isoformat(timespec="seconds") if start else None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="tradingagents-scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(15):
            try:
                self.tick(datetime.datetime.now())
            except Exception:  # a bad schedule must not stop the others
                logger.exception("Scheduler tick failed")

    def tick(self, now: datetime.datetime):
        """Queue every schedule whose start has come."""
        with self._lock:
            for schedule in self.schedules.values():
                due = schedule.get("next_run_at")
                if not schedule.get("enabled") or not due:
                    continue
                start = datetime.datetime.fromisoformat(due)
                if start > now:
                    continue
                # A start more than an hour gone was missed (the machine slept); skip it.
                if now - start <= datetime.timedelta(hours=1):
                    self._fire(schedule, start)
                self._plan(schedule, now)
            self._save()

    def _fire(self, schedule: dict, start: datetime.datetime) -> list[dict]:
        settings = schedule.get("settings") or self.defaults()
        date = analysis_date_for(schedule, start)
        jobs, errors = [], []
        for ticker in schedule["tickers"]:
            try:
                jobs.append(make_job(ticker, date, settings, schedule["id"]))
            except ValueError as exc:
                errors.append(f"{ticker}: {exc}")
        self.jobs.submit(jobs)
        schedule["last_run_at"] = now_iso()
        schedule["history"] = ([{"at": now_iso(), "date": date, "jobs": [j["id"] for j in jobs],
                                 "errors": errors}] + schedule.get("history", []))[:20]
        return jobs

    # --- what the page asks for -----------------------------------------------------

    def save(self, values: dict, schedule_id: str | None = None) -> dict:
        with self._lock:
            existing = self.schedules.get(schedule_id) if schedule_id else None
            if schedule_id and existing is None:
                raise KeyError(schedule_id)
            schedule = clean_schedule(values, self.defaults(), existing)
            self._plan(schedule, datetime.datetime.now())
            self.schedules[schedule["id"]] = schedule
            self._save()
            return schedule

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict:
        with self._lock:
            schedule = self.schedules[schedule_id]
            schedule["enabled"] = enabled
            self._plan(schedule, datetime.datetime.now())
            self._save()
            return schedule

    def run_now(self, schedule_id: str) -> list[dict]:
        with self._lock:
            jobs = self._fire(self.schedules[schedule_id], datetime.datetime.now())
            self._save()
            return jobs

    def delete(self, schedule_id: str):
        with self._lock:
            self.schedules.pop(schedule_id, None)
            self._save()

    def upcoming(self) -> list[dict]:
        planned = [s for s in self.schedules.values() if s.get("next_run_at")]
        return sorted(planned, key=lambda s: s["next_run_at"])
