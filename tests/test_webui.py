"""The web UI: its job queue with pause and cancel, its schedules, and its API."""

import datetime
import threading
import time

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from cli.display import MessageBuffer, apply_chunk
from cli.stats_handler import StatsCallbackHandler
from webui import runner, schedules
from webui.runner import JobManager, Live, RunCancelled, RunControl, make_job
from webui.server import create_app
from webui.settings import builtin_defaults, clean_settings
from webui.store import JsonStore

pytestmark = pytest.mark.unit


@pytest.fixture
def settings():
    return clean_settings({"llm_provider": "claude_cli", "quick_think_llm": "haiku",
                           "deep_think_llm": "sonnet", "analysts": ["market", "news"]}, builtin_defaults())


@pytest.fixture(autouse=True)
def _providers_ready(monkeypatch):
    monkeypatch.setattr(runner, "provider_problem", lambda provider: None)


def _wait(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _live():
    buffer = MessageBuffer()
    buffer.init_for_analysis(["market"])
    return Live(buffer, StatsCallbackHandler(), RunControl())


# --- pausing and cancelling a run ---------------------------------------------------


def test_a_paused_run_waits_at_its_next_call_and_goes_on_when_resumed():
    control = RunControl()
    control.pause()
    passed = threading.Event()
    threading.Thread(target=lambda: (control.on_chat_model_start({}, []), passed.set()), daemon=True).start()
    assert not passed.wait(0.3)
    control.resume()
    assert passed.wait(2)


def test_a_cancelled_run_fails_its_next_call_even_while_paused():
    control = RunControl()
    control.pause()
    control.cancel()
    with pytest.raises(RunCancelled):
        control.on_tool_start({}, "")


# --- the queue ----------------------------------------------------------------------


def test_jobs_run_in_order_and_record_their_result(tmp_path, settings):
    ran = []

    def execute(job, manager):
        manager.attach(job["id"], _live())
        ran.append(job["ticker"])
        return {"rating": "Buy"}

    manager = JobManager(JsonStore(tmp_path), execute=execute)
    manager.start()
    jobs = manager.submit([make_job("NVDA", "2026-10-20", settings), make_job("AAPL", "2026-10-20", settings)])
    assert _wait(lambda: all(manager.get(j["id"])["status"] == "completed" for j in jobs))
    manager.stop()
    assert ran == ["NVDA", "AAPL"]
    assert manager.get(jobs[0]["id"])["rating"] == "Buy"
    assert manager.get(jobs[0]["id"])["duration_s"] is not None


def test_a_failing_run_is_marked_failed_and_the_queue_goes_on(tmp_path, settings):
    def execute(job, manager):
        if job["ticker"] == "NVDA":
            raise RuntimeError("model down")
        return {}

    manager = JobManager(JsonStore(tmp_path), execute=execute)
    manager.start()
    bad, good = manager.submit([make_job("NVDA", "2026-10-20", settings), make_job("AAPL", "2026-10-20", settings)])
    assert _wait(lambda: manager.get(good["id"])["status"] == "completed")
    manager.stop()
    assert manager.get(bad["id"])["status"] == "failed"
    assert "model down" in manager.get(bad["id"])["error"]


def test_a_run_pauses_resumes_and_cancels(tmp_path, settings):
    calls = []

    def execute(job, manager):
        live = manager.attach(job["id"], _live())
        while True:
            live.control.gate()
            calls.append(1)
            time.sleep(0.01)

    manager = JobManager(JsonStore(tmp_path), execute=execute)
    manager.start()
    (job,) = manager.submit([make_job("NVDA", "2026-10-20", settings)])
    assert _wait(lambda: len(calls) > 3)
    manager.pause(job["id"])
    assert manager.get(job["id"])["status"] == "paused"
    time.sleep(0.1)
    count = len(calls)
    time.sleep(0.2)
    assert len(calls) <= count + 1
    manager.resume(job["id"])
    assert _wait(lambda: len(calls) > count + 3)
    manager.cancel(job["id"])
    assert _wait(lambda: manager.get(job["id"])["status"] == "cancelled")
    manager.stop()


def test_a_paused_queue_holds_its_jobs(tmp_path, settings):
    manager = JobManager(JsonStore(tmp_path), execute=lambda job, m: {})
    manager.set_queue_paused(True)
    manager.start()
    (job,) = manager.submit([make_job("NVDA", "2026-10-20", settings)])
    time.sleep(0.3)
    assert manager.get(job["id"])["status"] == "queued"
    manager.set_queue_paused(False)
    assert _wait(lambda: manager.get(job["id"])["status"] == "completed")
    manager.stop()


def test_a_run_left_running_at_shutdown_reads_as_interrupted(tmp_path, settings):
    store = JsonStore(tmp_path)
    job = make_job("NVDA", "2026-10-20", settings)
    job["status"] = "running"
    store.save("jobs", [job])
    manager = JobManager(store)
    assert manager.get(job["id"])["status"] == "interrupted"
    retried = manager.retry(job["id"])
    assert retried["status"] == "queued" and retried["retry_of"] == job["id"]


def test_a_queued_job_moves_up(tmp_path, settings):
    manager = JobManager(JsonStore(tmp_path))
    a, b = manager.submit([make_job("NVDA", "2026-10-20", settings), make_job("AAPL", "2026-10-20", settings)])
    manager.move(b["id"], -1)
    assert manager._next_job()["id"] == b["id"]


def test_a_bad_ticker_or_a_future_date_is_refused(settings):
    with pytest.raises(ValueError):
        make_job("not a ticker!", "2026-10-20", settings)
    with pytest.raises(ValueError):
        make_job("NVDA", (datetime.date.today() + datetime.timedelta(days=2)).isoformat(), settings)


def test_crypto_runs_without_the_fundamentals_analyst(settings):
    job = make_job("BTC-USD", "2026-10-20", {**settings, "analysts": ["market", "fundamentals"]})
    assert job["settings"]["analysts"] == ["market"]


# --- the terminal's progress, shared with the UI ------------------------------------


def test_apply_chunk_moves_the_teams_on():
    buffer = MessageBuffer()
    buffer.init_for_analysis(["market"])
    apply_chunk(buffer, {"market_report": "up"})
    assert buffer.agent_status["Market Analyst"] == "completed"
    assert buffer.agent_status["Bull Researcher"] == "in_progress"
    apply_chunk(buffer, {"investment_debate_state": {"bull_history": "b", "bear_history": "x"},
                         "investment_plan": "Buy it"})
    assert buffer.agent_status["Research Manager"] == "completed"
    assert buffer.agent_status["Trader"] == "in_progress"
    apply_chunk(buffer, {"risk_debate_state": {"aggressive_history": "go"}, "final_trade_decision": "Rating: Buy"})
    assert buffer.agent_status["Portfolio Manager"] == "completed"


# --- schedules ----------------------------------------------------------------------


def test_the_next_start_is_the_next_matching_weekday_and_time():
    schedule = {"weekdays": [0, 2], "times": ["08:30", "22:00"], "enabled": True}
    monday_noon = datetime.datetime(2026, 10, 26, 12, 0)   # a Monday
    assert schedules.next_run(schedule, monday_noon) == datetime.datetime(2026, 10, 26, 22, 0)
    assert schedules.next_run(schedule, datetime.datetime(2026, 10, 26, 22, 0)) == \
        datetime.datetime(2026, 10, 28, 8, 30)


def test_a_morning_run_analyses_the_previous_trading_day():
    monday = datetime.datetime(2026, 10, 26, 8, 0)
    assert schedules.analysis_date_for({"date_mode": "previous_day"}, monday) == "2026-10-23"
    assert schedules.analysis_date_for({"date_mode": "same_day"}, monday) == "2026-10-26"


def test_a_due_schedule_queues_its_watchlist_and_a_missed_one_is_skipped(tmp_path, settings):
    store = JsonStore(tmp_path)
    manager = JobManager(store)
    scheduler = schedules.Scheduler(store, manager, lambda: settings)
    s = scheduler.save({"tickers": "NVDA, AAPL", "times": "22:00", "weekdays": [0, 1, 2, 3, 4, 5, 6]})
    due = datetime.datetime.fromisoformat(s["next_run_at"])

    scheduler.tick(due + datetime.timedelta(minutes=1))
    tickers = sorted(j["ticker"] for j in manager.jobs.values())
    assert tickers == ["AAPL", "NVDA"]
    assert all(j["analysis_date"] == due.date().isoformat() for j in manager.jobs.values())
    assert datetime.datetime.fromisoformat(scheduler.schedules[s["id"]]["next_run_at"]) > due

    later = datetime.datetime.fromisoformat(scheduler.schedules[s["id"]]["next_run_at"])
    scheduler.tick(later + datetime.timedelta(hours=3))
    assert len(manager.jobs) == 2


def test_a_schedule_needs_tickers_times_and_weekdays(tmp_path, settings):
    store = JsonStore(tmp_path)
    scheduler = schedules.Scheduler(store, JobManager(store), lambda: settings)
    for bad in ({"tickers": "", "times": "22:00", "weekdays": [0]},
                {"tickers": "NVDA", "times": "25:99", "weekdays": [0]},
                {"tickers": "NVDA", "times": "22:00", "weekdays": []}):
        with pytest.raises(ValueError):
            scheduler.save(bad)


# --- the API ------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path, start=False)
    with TestClient(app, base_url="http://127.0.0.1:8765", headers={"X-TradingAgents": "1"}) as client:
        yield client


def test_a_change_without_the_page_header_is_refused(tmp_path):
    with TestClient(create_app(tmp_path, start=False), base_url="http://127.0.0.1:8765") as bare:
        assert bare.get("/api/options").status_code == 200
        assert bare.post("/api/jobs", json={"tickers": "NVDA"}).status_code == 403


def test_a_request_under_another_host_name_is_refused(tmp_path):
    app = create_app(tmp_path, start=False)
    with TestClient(app, base_url="http://evil.example:8765") as other:
        assert other.get("/api/keys").status_code == 403
    with TestClient(create_app(tmp_path, start=False, allowed_hosts=None),
                    base_url="http://192.168.1.5:8765") as lan:
        assert lan.get("/api/options").status_code == 200


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "keys.env"
    monkeypatch.setenv("TRADINGAGENTS_ENV_FILE", str(path))
    for name in ("FRED_API_KEY", "SEC_EDGAR_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    return path


def _key(status, name):
    return next(k for g in status["groups"] for k in g["keys"] if k["name"] == name)


def test_a_key_is_saved_to_the_env_file_and_used_at_once(client, env_file):
    import os

    status = client.put("/api/keys/FRED_API_KEY", json={"value": " abcdef123456 "}).json()
    fred = _key(status, "FRED_API_KEY")
    assert fred["set"] and fred["source"] == "file"
    assert fred["preview"] == "••••3456"
    assert "abcdef123456" not in client.get("/api/keys").text
    assert os.environ["FRED_API_KEY"] == "abcdef123456"
    assert "FRED_API_KEY='abcdef123456'" in env_file.read_text()

    sec = _key(client.put("/api/keys/SEC_EDGAR_USER_AGENT", json={"value": "Ann Lee ann@example.com"}).json(),
               "SEC_EDGAR_USER_AGENT")
    assert sec["preview"] == "Ann Lee ann@example.com"   # not a secret, shown whole

    status = client.delete("/api/keys/FRED_API_KEY").json()
    assert not _key(status, "FRED_API_KEY")["set"]
    assert "FRED_API_KEY" not in os.environ
    assert "FRED_API_KEY" not in env_file.read_text()


def test_only_known_keys_on_one_line_are_saved(client, env_file):
    assert client.put("/api/keys/PATH", json={"value": "x"}).status_code == 400
    assert client.put("/api/keys/FRED_API_KEY", json={"value": "a\nEVIL=1"}).status_code == 400
    assert client.put("/api/keys/FRED_API_KEY", json={"value": "  "}).status_code == 400
    assert not env_file.exists()


def test_the_key_page_lists_providers_and_data_sources(client, env_file):
    groups = {g["group"]: [k["name"] for k in g["keys"]] for g in client.get("/api/keys").json()["groups"]}
    assert "ANTHROPIC_API_KEY" in groups["LLM-Provider"]
    assert {"FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "TYPESAFE_API_KEY", "SEC_EDGAR_USER_AGENT"} <= set(
        groups["Datenquellen"])


def test_the_page_and_its_choices_are_served(client):
    assert "TradingAgents" in client.get("/").text
    options = client.get("/api/options").json()
    assert any(p["key"] == "claude_cli" for p in options["providers"])
    assert client.get("/static/app.js").status_code == 200


def test_settings_are_saved_and_cleaned(client):
    saved = client.put("/api/settings", json={"llm_provider": "Claude_CLI", "research_depth": 99,
                                              "analysts": ["news", "bogus"], "unknown": 1}).json()
    assert saved["llm_provider"] == "claude_cli"
    assert saved["research_depth"] == 10
    assert saved["analysts"] == ["news"]
    assert "unknown" not in saved
    assert client.get("/api/settings").json()["llm_provider"] == "claude_cli"


def test_a_job_is_queued_and_shown(client):
    jobs = client.post("/api/jobs", json={"tickers": "nvda, aapl", "analysis_date": "2026-10-20"}).json()
    assert [j["ticker"] for j in jobs] == ["NVDA", "AAPL"]
    overview = client.get("/api/overview").json()
    assert len(overview["queued"]) == 2
    detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
    assert detail["status"] == "queued"
    assert detail["agents"][0]["team"] == "Analyst Team"
    assert client.post(f"/api/jobs/{jobs[0]['id']}/cancel").json()["status"] == "cancelled"
    assert client.delete(f"/api/jobs/{jobs[0]['id']}").status_code == 200
    assert client.get(f"/api/jobs/{jobs[0]['id']}").status_code == 404


def test_a_bad_job_is_refused(client):
    assert client.post("/api/jobs", json={"tickers": ""}).status_code == 400
    assert client.post("/api/jobs", json={"tickers": "NVDA", "analysis_date": "2999-01-01"}).status_code == 400


def test_schedules_are_created_switched_and_run(client):
    s = client.post("/api/schedules", json={"tickers": "NVDA", "times": "22:00", "weekdays": [0, 1, 2, 3, 4]}).json()
    assert s["next_run_at"]
    off = client.post(f"/api/schedules/{s['id']}/enabled", json={"enabled": False}).json()
    assert off["next_run_at"] is None
    assert len(client.post(f"/api/schedules/{s['id']}/run").json()) == 1
    assert client.get("/api/overview").json()["queued"][0]["schedule_id"] == s["id"]


def test_report_files_outside_the_reports_folder_are_refused(client):
    assert client.get("/reports/..%2F..%2Fsecrets/complete_report.html").status_code == 404
    assert client.get("/reports/NVDA_20261020_220000/../../x").status_code == 404
