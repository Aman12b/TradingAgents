"""Runs the UI's analyses one at a time, in order, with their live progress kept for the page.

A run can be paused and cancelled. Both act through a callback every model and
tool call passes: while paused, the next call waits; once cancelled, it raises.
A call already under way finishes first, so a pause takes hold within one call.
"""

import datetime
import json
import logging
import threading
import time
import traceback
import uuid
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from cli.display import (
    MessageBuffer,
    apply_chunk,
    record_messages,
)
from cli.models import AnalystType
from cli.prompts import (
    detect_asset_type,
    filter_analysts_for_asset_type,
    parse_analysis_date,
    parse_ticker,
)
from cli.stats_handler import StatsCallbackHandler
from tradingagents.agents.rating import run_rating
from tradingagents.dataflows.symbols import safe_ticker_component

from .settings import ANALYSTS, provider_problem, run_config
from .store import JsonStore

logger = logging.getLogger(__name__)

ANALYST_ORDER = [key for key, _ in ANALYSTS]

# Statuses a job moves through. A job left running when the server stopped is
# "interrupted"; retrying it resumes from its checkpoint when it kept one.
QUEUED, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED, INTERRUPTED = (
    "queued", "running", "paused", "completed", "failed", "cancelled", "interrupted")
ACTIVE = (RUNNING, PAUSED)
FINISHED = (COMPLETED, FAILED, CANCELLED, INTERRUPTED)


def now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class RunCancelled(Exception):
    """The run was cancelled from the UI."""


class RunControl(BaseCallbackHandler):
    """Holds each model and tool call while the run is paused; fails it once cancelled."""

    raise_error = True

    def __init__(self):
        super().__init__()
        self._running = threading.Event()
        self._running.set()
        self.cancelled = False

    @property
    def paused(self) -> bool:
        return not self._running.is_set()

    def pause(self):
        self._running.clear()

    def resume(self):
        self._running.set()

    def cancel(self):
        self.cancelled = True
        self._running.set()

    def gate(self):
        """Wait while paused; raise once cancelled."""
        while not self._running.wait(0.5):
            pass
        if self.cancelled:
            raise RunCancelled("cancelled")

    def on_chat_model_start(self, *args, **kwargs):
        self.gate()

    def on_llm_start(self, *args, **kwargs):
        self.gate()

    def on_tool_start(self, *args, **kwargs):
        self.gate()


class LoggedBuffer(MessageBuffer):
    """The terminal's message buffer, also writing each entry to the run's log file."""

    def __init__(self, log_file: Path, max_length=400):
        super().__init__(max_length=max_length)
        self.log_file = log_file

    def _log(self, line: str):
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def add_message(self, message_type, content):
        super().add_message(message_type, content)
        timestamp, _, _ = self.messages[-1]
        self._log(f"{timestamp} [{message_type}] {str(content).replace(chr(10), ' ')}")

    def add_tool_call(self, tool_name, args):
        super().add_tool_call(tool_name, args)
        timestamp, _, _ = self.tool_calls[-1]
        args_str = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
        self._log(f"{timestamp} [Tool Call] {tool_name}({args_str})")


# The parts of a run's state the page shows; saved with a finished run so it can be read later.
STATE_KEYS = ("market_report", "sentiment_report", "news_report", "fundamentals_report",
              "investment_debate_state", "investment_plan", "trader_investment_plan",
              "risk_debate_state", "final_trade_decision")


def _shown_state(state: dict) -> dict:
    shown = {}
    for key in STATE_KEYS:
        value = state.get(key)
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if isinstance(v, str)}
        if value:
            shown[key] = value
    return shown


class Live:
    """A running job's progress: the terminal's buffer and counters, and the state so far."""

    def __init__(self, buffer: MessageBuffer, stats: StatsCallbackHandler, control: RunControl):
        self.buffer = buffer
        self.stats = stats
        self.control = control
        self.state: dict = {}
        self.rev = 0
        self.lock = threading.Lock()
        self.paused_at: float | None = None
        self.paused_total = 0.0


def make_job(ticker: str, analysis_date: str, settings: dict, schedule_id: str | None = None) -> dict:
    """A queued job for ``ticker`` on ``analysis_date``; a bad ticker or date raises ValueError."""
    ticker = parse_ticker(ticker)
    analysis_date = parse_analysis_date(analysis_date)
    asset_type = detect_asset_type(ticker)
    analysts = filter_analysts_for_asset_type([AnalystType(a) for a in settings["analysts"]], asset_type)
    if not analysts:
        raise ValueError(f"No analyst selected that can analyse {ticker}")
    return {
        "id": uuid.uuid4().hex[:12],
        "ticker": ticker,
        "analysis_date": analysis_date,
        "asset_type": asset_type.value,
        "settings": {**settings, "analysts": [a.value for a in analysts]},
        "schedule_id": schedule_id,
        "status": QUEUED,
        "created_at": now_iso(),
        # Place in the queue: lower runs first.
        "order": time.time(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "rating": None,
        "stats": None,
        "duration_s": None,
        "run_dir": None,
        "report_dir": None,
    }


class JobManager:
    """The queue of jobs and the one worker thread that runs them."""

    def __init__(self, store: JsonStore, execute=None):
        self.store = store
        self.jobs: dict[str, dict] = {}
        self.live: dict[str, Live] = {}
        self.queue_paused = False
        self._execute = execute or self._run_graph
        self._cond = threading.Condition()
        self._stop = False
        self._worker: threading.Thread | None = None
        for job in store.load("jobs", []):
            if job.get("status") in ACTIVE:
                job["status"] = INTERRUPTED
                job["error"] = job.get("error") or "The server stopped during this run."
            self.jobs[job["id"]] = job
        self.queue_paused = bool(store.load("queue", {}).get("paused", False))
        self._save()

    # --- persistence ----------------------------------------------------------------

    def _save(self):
        jobs = sorted(self.jobs.values(), key=lambda j: j["created_at"])
        self.store.save("jobs", jobs)
        self.store.save("queue", {"paused": self.queue_paused})

    # --- the worker -----------------------------------------------------------------

    def start(self):
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="tradingagents-worker", daemon=True)
            self._worker.start()

    def stop(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        for live in list(self.live.values()):
            live.control.cancel()

    def _next_job(self) -> dict | None:
        if self.queue_paused:
            return None
        queued = [j for j in self.jobs.values() if j["status"] == QUEUED]
        return min(queued, key=lambda j: j.get("order", 0)) if queued else None

    def _work(self):
        while True:
            with self._cond:
                while not self._stop and self._next_job() is None:
                    self._cond.wait(1.0)
                if self._stop:
                    return
                job = self._next_job()
                job["status"] = RUNNING
                job["started_at"] = now_iso()
                job["error"] = None
                self._save()
            self._run(job)

    def _run(self, job: dict):
        start = time.time()
        try:
            problem = provider_problem(job["settings"]["llm_provider"])
            if problem:
                raise RuntimeError(problem)
            result = self._execute(job, self)
            job.update(result or {})
            job["status"] = COMPLETED
        except RunCancelled:
            job["status"] = CANCELLED
        except Exception as exc:  # any failure ends this job, never the worker
            logger.exception("Run %s failed", job["id"])
            job["status"] = FAILED
            job["error"] = f"{type(exc).__name__}: {exc}"
            job["traceback"] = traceback.format_exc()[-4000:]
        finally:
            live = self.live.get(job["id"])
            if live is not None:
                job["stats"] = live.stats.get_stats()
                paused = live.paused_total + (time.time() - live.paused_at if live.paused_at else 0)
                job["duration_s"] = round(time.time() - start - paused, 1)
            job["finished_at"] = now_iso()
            with self._cond:
                self._save()

    # --- one run --------------------------------------------------------------------

    @staticmethod
    def _run_graph(job: dict, manager: "JobManager") -> dict:
        """Run the analysis for ``job`` as the terminal does, keeping its progress live."""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        settings = job["settings"]
        config = run_config(settings)
        ticker, date, asset_type = job["ticker"], job["analysis_date"], job["asset_type"]
        keys = [a for a in ANALYST_ORDER if a in settings["analysts"]]

        run_dir = Path(config["results_dir"]) / safe_ticker_component(ticker) / date
        run_dir.mkdir(parents=True, exist_ok=True)
        job["run_dir"] = str(run_dir)
        buffer = LoggedBuffer(run_dir / "message_tool.log")
        buffer.init_for_analysis(keys)
        stats, control = StatsCallbackHandler(), RunControl()
        live = manager.attach(job["id"], Live(buffer, stats, control))

        def note(text):
            with live.lock:
                buffer.add_message("System", text)
                live.rev += 1

        note(f"Analysing {ticker} ({asset_type}) on {date} with {', '.join(keys)}")
        note(f"Models: {settings['quick_think_llm']} (quick), {settings['deep_think_llm']} (deep) "
             f"on {settings['llm_provider']}")

        graph = TradingAgentsGraph(keys, config=config, debug=True, callbacks=[stats, control])
        for key in keys:
            buffer.update_agent_status(dict(ANALYSTS)[key], "in_progress")

        init_state = graph.create_run_state(ticker, date, asset_type, None)
        args = graph.propagator.get_graph_args(callbacks=[stats, control])
        thread_id = graph.begin_checkpoint(ticker, date, asset_type, None)
        if thread_id is not None:
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread_id
            note("Resuming the saved run" if getattr(graph, "_resuming", False) else "Starting fresh")

        try:
            for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
                with live.lock:
                    record_messages(buffer, messages)
                    if chunk is not None:
                        apply_chunk(buffer, chunk)
                        live.state.update(chunk)
                    live.rev += 1
                control.gate()
            final_state = dict(live.state)
            graph.record_decision(ticker, date, final_state)
            graph.clear_checkpoint_on_success(ticker, date, asset_type, None)
        finally:
            graph.end_checkpoint()

        with live.lock:
            for agent in buffer.agent_status:
                buffer.update_agent_status(agent, "completed")
            for section in buffer.report_sections:
                if final_state.get(section):
                    buffer.update_report_section(section, final_state[section])
            buffer.add_message("System", f"Completed analysis of {ticker} for {date}")
            live.rev += 1

        report_dir = graph.default_report_path(ticker)
        graph.save_reports(final_state, ticker, report_dir, html=True)
        (report_dir / "state.json").write_text(json.dumps(_shown_state(final_state), indent=2),
                                               encoding="utf-8")
        return {"rating": run_rating(final_state), "report_dir": str(report_dir)}

    def attach(self, job_id: str, live: Live) -> Live:
        self.live[job_id] = live
        return live

    # --- what the page asks for -----------------------------------------------------

    def submit(self, jobs: list[dict]) -> list[dict]:
        with self._cond:
            for job in jobs:
                self.jobs[job["id"]] = job
            self._save()
            self._cond.notify_all()
        return jobs

    def get(self, job_id: str) -> dict:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def pause(self, job_id: str):
        job, live = self.get(job_id), self.live.get(job_id)
        if job["status"] == RUNNING and live is not None:
            live.control.pause()
            live.paused_at = time.time()
            job["status"] = PAUSED
            with self._cond:
                self._save()

    def resume(self, job_id: str):
        job, live = self.get(job_id), self.live.get(job_id)
        if job["status"] == PAUSED and live is not None:
            if live.paused_at:
                live.paused_total += time.time() - live.paused_at
                live.paused_at = None
            live.control.resume()
            job["status"] = RUNNING
            with self._cond:
                self._save()

    def cancel(self, job_id: str):
        job = self.get(job_id)
        with self._cond:
            if job["status"] == QUEUED:
                job["status"] = CANCELLED
                job["finished_at"] = now_iso()
                self._save()
            elif job["status"] in ACTIVE and job_id in self.live:
                self.live[job_id].control.cancel()

    def retry(self, job_id: str) -> dict:
        old = self.get(job_id)
        job = make_job(old["ticker"], old["analysis_date"], old["settings"], old.get("schedule_id"))
        job["retry_of"] = job_id
        return self.submit([job])[0]

    def delete(self, job_id: str):
        job = self.get(job_id)
        if job["status"] in ACTIVE:
            raise ValueError("Cancel the run before deleting it.")
        with self._cond:
            self.jobs.pop(job_id, None)
            self.live.pop(job_id, None)
            self._save()

    def move(self, job_id: str, step: int):
        """Move a queued job earlier (step < 0) or later in the queue."""
        with self._cond:
            queued = sorted((j for j in self.jobs.values() if j["status"] == QUEUED),
                            key=lambda j: j.get("order", 0))
            ids = [j["id"] for j in queued]
            if job_id not in ids:
                return
            i = ids.index(job_id)
            k = max(0, min(len(ids) - 1, i + step))
            if i == k:
                return
            queued[i]["order"], queued[k]["order"] = queued[k]["order"], queued[i]["order"]
            self._save()

    def set_queue_paused(self, paused: bool):
        with self._cond:
            self.queue_paused = paused
            self._save()
            self._cond.notify_all()

    def current(self) -> dict | None:
        active = [j for j in self.jobs.values() if j["status"] in ACTIVE]
        return active[0] if active else None
