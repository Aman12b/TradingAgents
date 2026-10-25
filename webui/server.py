"""The UI's web server: a JSON API over the job queue, the schedules and past results, and the page."""

import datetime
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from cli.display import MessageBuffer

from . import history
from .runner import ACTIVE, COMPLETED, JobManager, make_job
from .schedules import WEEKDAYS, Scheduler
from .settings import ANALYSTS, builtin_defaults, clean_settings, options, provider_status
from .store import DEFAULT_DIR, JsonStore

STATIC = Path(__file__).parent / "static"

# The report's parts in reading order: (state path, agent, team).
SECTIONS = [
    (("market_report",), "Market Analyst", "Analysts"),
    (("sentiment_report",), "Sentiment Analyst", "Analysts"),
    (("news_report",), "News Analyst", "Analysts"),
    (("fundamentals_report",), "Fundamentals Analyst", "Analysts"),
    (("investment_debate_state", "bull_history"), "Bull Researcher", "Research"),
    (("investment_debate_state", "bear_history"), "Bear Researcher", "Research"),
    (("investment_plan",), "Research Manager", "Research"),
    (("trader_investment_plan",), "Trader", "Trading"),
    (("risk_debate_state", "aggressive_history"), "Aggressive Analyst", "Risk"),
    (("risk_debate_state", "conservative_history"), "Conservative Analyst", "Risk"),
    (("risk_debate_state", "neutral_history"), "Neutral Analyst", "Risk"),
    (("final_trade_decision",), "Portfolio Manager", "Portfolio"),
]

TEAMS = [("Analyst Team", None), *MessageBuffer.FIXED_AGENTS.items()]


def _elapsed(job: dict, live) -> float | None:
    if job.get("duration_s") is not None:
        return job["duration_s"]
    if not job.get("started_at"):
        return None
    started = datetime.datetime.fromisoformat(job["started_at"]).timestamp()
    paused = 0.0
    if live is not None:
        paused = live.paused_total + (time.time() - live.paused_at if live.paused_at else 0)
    return round(time.time() - started - paused, 1)


def summary(job: dict, live=None) -> dict:
    settings = job["settings"]
    shown = {k: job.get(k) for k in ("id", "ticker", "analysis_date", "asset_type", "status", "created_at",
                                     "started_at", "finished_at", "error", "rating", "schedule_id",
                                     "report_dir", "retry_of")}
    shown.update({
        "provider": settings["llm_provider"],
        "quick_model": settings["quick_think_llm"],
        "deep_model": settings["deep_think_llm"],
        "analysts": settings["analysts"],
        "depth": settings["research_depth"],
        "elapsed_s": _elapsed(job, live),
        "report_name": Path(job["report_dir"]).name if job.get("report_dir") else None,
        "stats": live.stats.get_stats() if live is not None and job["status"] in ACTIVE else job.get("stats"),
    })
    if live is not None:
        statuses = list(live.buffer.agent_status.values())
        shown["progress"] = {"done": statuses.count("completed"), "total": len(statuses)}
    return shown


def _agents(job: dict, live) -> list[dict]:
    """The agent board, team by team, with each agent's status."""
    names = dict(ANALYSTS)
    status = dict(live.buffer.agent_status) if live is not None else {}
    finished = job["status"] == COMPLETED
    board = []
    for team, members in TEAMS:
        members = members or [names[k] for k in job["settings"]["analysts"]]
        board.append({"team": team, "agents": [
            {"name": m, "status": status.get(m, "completed" if finished else "pending")} for m in members
        ]})
    return board


def _sections(state: dict) -> list[dict]:
    shown = []
    for path, agent, team in SECTIONS:
        value = state
        for key in path:
            value = (value or {}).get(key) if isinstance(value, dict) else None
        if value:
            shown.append({"key": ".".join(path), "agent": agent, "team": team, "html": history.markdown(value)})
    return shown


def _feed(buffer: MessageBuffer, limit: int = 250) -> list[dict]:
    """Messages and tool calls together, oldest first, as the terminal's log shows them."""
    items = [{"time": t, "kind": kind, "text": str(text)[:4000]} for t, kind, text in buffer.messages]
    items += [{"time": t, "kind": "Tool", "text": f"{name}({', '.join(f'{k}={v}' for k, v in (args or {}).items())})"}
              for t, name, args in buffer.tool_calls]
    items.sort(key=lambda i: i["time"])
    return items[-limit:]


def detail(manager: JobManager, job_id: str, rev: int | None = None) -> dict:
    job = manager.get(job_id)
    live = manager.live.get(job_id)
    shown = summary(job, live)
    shown["settings"] = job["settings"]
    shown["traceback"] = job.get("traceback")
    shown["agents"] = _agents(job, live)
    if live is not None:
        with live.lock:
            shown["rev"] = live.rev
            shown["feed"] = _feed(live.buffer)
            shown["current_report"] = history.markdown(live.buffer.current_report)
            state = dict(live.state)
    else:
        shown["rev"] = -1
        shown["feed"] = []
        shown["current_report"] = ""
        state = history.saved_state(job.get("report_dir"))
    if rev is None or rev != shown["rev"]:
        shown["sections"] = _sections(state)
    return shown


def create_app(store_dir: Path | str = DEFAULT_DIR, start: bool = True, execute=None) -> FastAPI:
    store = JsonStore(store_dir)

    def defaults() -> dict:
        return clean_settings(store.load("settings", {}), builtin_defaults())

    manager = JobManager(store, execute=execute)
    scheduler = Scheduler(store, manager, defaults)

    @asynccontextmanager
    async def lifespan(app):
        if start:
            manager.start()
            scheduler.start()
        yield
        scheduler.stop()
        manager.stop()

    app = FastAPI(title="TradingAgents", lifespan=lifespan)
    app.state.manager, app.state.scheduler, app.state.store = manager, scheduler, store

    def job_or_404(job_id):
        try:
            return manager.get(job_id)
        except KeyError:
            raise HTTPException(404, "No such run") from None

    # --- page and choices -----------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/options")
    def get_options():
        return {**options(), "weekdays": WEEKDAYS}

    @app.get("/api/settings")
    def get_settings():
        return defaults()

    @app.put("/api/settings")
    def put_settings(values: Annotated[dict, Body()]):
        settings = clean_settings(values, defaults())
        store.save("settings", settings)
        return settings

    @app.post("/api/settings/reset")
    def reset_settings():
        store.save("settings", {})
        return defaults()

    @app.get("/api/providers/{provider}")
    def get_provider(provider: str):
        return provider_status(provider)

    @app.get("/api/paths")
    def get_paths():
        from tradingagents.default_config import build_default_config

        config = build_default_config()
        return {"results_dir": config["results_dir"], "memory_log": config["memory_log_path"],
                "cache_dir": config["data_cache_dir"], "ui_dir": str(store.folder)}

    # --- overview -------------------------------------------------------------------

    @app.get("/api/overview")
    def overview():
        jobs = sorted(manager.jobs.values(), key=lambda j: j["created_at"], reverse=True)
        current = manager.current()
        queued = sorted((j for j in jobs if j["status"] == "queued"), key=lambda j: j.get("order", 0))
        return {
            "now": datetime.datetime.now().isoformat(timespec="seconds"),
            "queue_paused": manager.queue_paused,
            "current": summary(current, manager.live.get(current["id"])) if current else None,
            "queued": [summary(j) for j in queued],
            "jobs": [summary(j, manager.live.get(j["id"])) for j in jobs[:300]],
            "upcoming": [{"id": s["id"], "name": s["name"], "next_run_at": s["next_run_at"],
                          "tickers": s["tickers"]} for s in scheduler.upcoming()[:5]],
        }

    # --- jobs -----------------------------------------------------------------------

    @app.post("/api/jobs")
    def post_jobs(body: Annotated[dict, Body()]):
        settings = clean_settings(body.get("settings") or {}, defaults())
        tickers = [t.strip() for t in str(body.get("tickers") or "").replace(";", ",").split(",") if t.strip()]
        if not tickers:
            raise HTTPException(400, "Enter at least one ticker.")
        date = str(body.get("analysis_date") or datetime.date.today().isoformat())
        try:
            jobs = [make_job(t, date, settings) for t in tickers]
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        if body.get("save_as_default"):
            store.save("settings", settings)
        return [summary(j) for j in manager.submit(jobs)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, rev: int | None = None):
        job_or_404(job_id)
        return detail(manager, job_id, rev)

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
    def get_job_log(job_id: str):
        job = job_or_404(job_id)
        log = Path(job["run_dir"]) / "message_tool.log" if job.get("run_dir") else None
        if log is None or not log.is_file():
            return ""
        return log.read_text(encoding="utf-8", errors="replace")[-400_000:]

    @app.post("/api/jobs/{job_id}/{action}")
    def job_action(job_id: str, action: str):
        job_or_404(job_id)
        try:
            if action == "pause":
                manager.pause(job_id)
            elif action == "resume":
                manager.resume(job_id)
            elif action == "cancel":
                manager.cancel(job_id)
            elif action == "retry":
                return summary(manager.retry(job_id))
            elif action in ("up", "down"):
                manager.move(job_id, -1 if action == "up" else 1)
            else:
                raise HTTPException(404, f"Unknown action {action}")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return summary(manager.get(job_id), manager.live.get(job_id))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        job_or_404(job_id)
        try:
            manager.delete(job_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"deleted": job_id}

    @app.post("/api/queue")
    def set_queue(body: Annotated[dict, Body()]):
        manager.set_queue_paused(bool(body.get("paused")))
        return {"queue_paused": manager.queue_paused}

    # --- schedules ------------------------------------------------------------------

    @app.get("/api/schedules")
    def get_schedules():
        return sorted(scheduler.schedules.values(), key=lambda s: s.get("created_at") or "")

    @app.post("/api/schedules")
    def post_schedule(body: Annotated[dict, Body()]):
        try:
            return scheduler.save(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/schedules/{schedule_id}")
    def put_schedule(schedule_id: str, body: Annotated[dict, Body()]):
        try:
            return scheduler.save(body, schedule_id)
        except KeyError:
            raise HTTPException(404, "No such schedule") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/schedules/{schedule_id}/enabled")
    def enable_schedule(schedule_id: str, body: Annotated[dict, Body()]):
        if schedule_id not in scheduler.schedules:
            raise HTTPException(404, "No such schedule")
        return scheduler.set_enabled(schedule_id, bool(body.get("enabled")))

    @app.post("/api/schedules/{schedule_id}/run")
    def run_schedule(schedule_id: str):
        if schedule_id not in scheduler.schedules:
            raise HTTPException(404, "No such schedule")
        return [summary(j) for j in scheduler.run_now(schedule_id)]

    @app.delete("/api/schedules/{schedule_id}")
    def delete_schedule(schedule_id: str):
        scheduler.delete(schedule_id)
        return {"deleted": schedule_id}

    # --- past results ---------------------------------------------------------------

    @app.get("/api/reports")
    def get_reports():
        return history.saved_reports()

    @app.get("/reports/{name}/{filename:path}", include_in_schema=False)
    def get_report_file(name: str, filename: str):
        try:
            path = history.report_file(name, filename)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "No such report") from None
        media = "text/markdown; charset=utf-8" if path.suffix == ".md" else None
        return FileResponse(path, media_type=media)

    @app.get("/api/decisions")
    def get_decisions():
        return history.decisions()

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
