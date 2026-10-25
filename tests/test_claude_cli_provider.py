"""The claude_cli provider: Claude through ``claude -p``, with tool calls and
structured output carried by the CLI's ``--json-schema``."""

import json
import subprocess

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from tradingagents.llm_clients import build_llm_kwargs, claude_cli_client as cc, create_llm_client
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.validators import validate_model

pytestmark = pytest.mark.unit


class FakeCLI:
    """Stands in for ``subprocess.run``: records each call and answers from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, command, *, input, cwd, **kwargs):
        system_file = command[command.index("--system-prompt-file") + 1]
        with open(system_file, encoding="utf-8") as f:
            system = f.read()
        schema = None
        if "--json-schema" in command:
            schema = json.loads(command[command.index("--json-schema") + 1])
        self.calls.append({"command": command, "prompt": input, "system": system, "schema": schema})
        answer = self.answers.pop(0)
        if isinstance(answer, subprocess.CompletedProcess):
            return answer
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(answer), stderr="")


def _result(text="", structured=None, **extra):
    result = {"type": "result", "is_error": False, "result": text, "session_id": "s",
              "usage": {"input_tokens": 3, "cache_read_input_tokens": 10, "output_tokens": 5}}
    if structured is not None:
        result["structured_output"] = structured
    return {**result, **extra}


@pytest.fixture
def llm(monkeypatch):
    monkeypatch.setenv(cc.CLI_PATH_ENV, "claude-test")
    return create_llm_client("claude_cli", "sonnet", effort="high").get_llm()


def _fake(monkeypatch, *answers):
    fake = FakeCLI(*answers)
    monkeypatch.setattr(cc.subprocess, "run", fake)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    return fake


@tool
def get_price(ticker: str) -> str:
    """The latest price of a ticker."""
    return "1"


def test_registered_as_a_keyless_provider_with_any_model():
    assert get_api_key_env("claude_cli") is None
    assert validate_model("claude_cli", "claude-opus-5-5")
    assert build_llm_kwargs({"llm_provider": "claude_cli", "anthropic_effort": "max"})["effort"] == "max"


def test_a_plain_call_runs_the_cli_headless_with_the_prompt_on_stdin(llm, monkeypatch):
    fake = _fake(monkeypatch, _result("Buy."))
    reply = llm.invoke([SystemMessage("Be brief."), HumanMessage("AAPL?")])

    assert reply.content == "Buy."
    assert reply.usage_metadata["input_tokens"] == 13
    call = fake.calls[0]
    command = call["command"]
    assert command[:2] == ["claude-test", "-p"]
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "high"
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command and "--json-schema" not in command
    assert call["prompt"] == "AAPL?"
    assert call["system"] == "Be brief."


def test_a_tool_turn_comes_back_as_tool_calls(llm, monkeypatch):
    fake = _fake(monkeypatch, _result(structured={
        "content": "", "tool_calls": [{"name": "get_price", "args": {"ticker": "AAPL"}}]}))
    reply = llm.bind_tools([get_price]).invoke("Price of AAPL?")

    assert reply.tool_calls[0]["name"] == "get_price"
    assert reply.tool_calls[0]["args"] == {"ticker": "AAPL"}
    assert reply.tool_calls[0]["id"].startswith("call_")
    schema = fake.calls[0]["schema"]
    offered = schema["properties"]["tool_calls"]["items"]["anyOf"]
    assert [c["properties"]["name"]["const"] for c in offered] == ["get_price"]
    assert "minItems" not in schema["properties"]["tool_calls"]
    assert "get_price: The latest price of a ticker." in fake.calls[0]["system"]


def test_a_forced_tool_choice_requires_a_call(llm, monkeypatch):
    fake = _fake(monkeypatch, _result(structured={
        "content": "", "tool_calls": [{"name": "get_price", "args": {"ticker": "X"}}]}))
    llm.bind_tools([get_price], tool_choice="any").invoke("go")
    assert fake.calls[0]["schema"]["properties"]["tool_calls"]["minItems"] == 1


def test_the_final_answer_of_a_tool_turn_is_its_content(llm, monkeypatch):
    _fake(monkeypatch, _result(structured={"content": "Report.", "tool_calls": []}))
    reply = llm.bind_tools([get_price]).invoke("go")
    assert reply.content == "Report." and reply.tool_calls == []


def test_a_conversation_with_tool_results_is_written_out(llm, monkeypatch):
    fake = _fake(monkeypatch, _result("Done."))
    llm.invoke([
        HumanMessage("Price?"),
        AIMessage("", tool_calls=[{"name": "get_price", "args": {"ticker": "AAPL"}, "id": "c1"}]),
        ToolMessage("187.2", tool_call_id="c1", name="get_price"),
    ])
    prompt = fake.calls[0]["prompt"]
    assert "<user>\nPrice?\n</user>" in prompt
    assert '[tool call get_price id=c1] {"ticker": "AAPL"}' in prompt
    assert '<tool_result name="get_price" id="c1">\n187.2\n</tool_result>' in prompt


class Decision(BaseModel):
    rating: str
    reason: str


def test_structured_output_uses_the_schema_and_parses_it(llm, monkeypatch):
    fake = _fake(monkeypatch, _result(structured={"rating": "Buy", "reason": "Growth"}))
    decision = llm.with_structured_output(Decision).invoke("Decide.")

    assert decision == Decision(rating="Buy", reason="Growth")
    assert fake.calls[0]["schema"]["properties"].keys() == {"rating", "reason"}


def test_a_failed_call_is_retried_then_reported(llm, monkeypatch):
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="Not logged in")
    fake = _fake(monkeypatch, failed, failed, failed)
    with pytest.raises(cc.ClaudeCLIError, match="Not logged in"):
        llm.invoke("hi")
    assert len(fake.calls) == 3  # the call and its two default retries


def test_an_error_result_is_not_taken_for_an_answer(llm, monkeypatch):
    _fake(monkeypatch, _result("Usage limit reached", is_error=True), _result("ok"))
    assert llm.invoke("hi").content == "ok"


def test_a_missing_cli_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(cc, "find_claude_cli", lambda: None)
    llm = create_llm_client("claude_cli", "sonnet").get_llm()
    with pytest.raises(cc.ClaudeCLINotFound, match="Claude Code CLI was not found"):
        llm.invoke("hi")


def test_the_cli_is_found_in_an_editor_extension(monkeypatch, tmp_path):
    monkeypatch.delenv(cc.CLI_PATH_ENV, raising=False)
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    monkeypatch.setattr(cc.Path, "home", lambda: tmp_path)
    name = "claude.exe" if cc.os.name == "nt" else "claude"
    for version in ("2.1.9", "2.1.310"):
        binary = tmp_path / f".vscode/extensions/anthropic.claude-code-{version}-x/resources/native-binary/{name}"
        binary.parent.mkdir(parents=True)
        binary.write_text("")
    assert "2.1.310" in cc.find_claude_cli()
