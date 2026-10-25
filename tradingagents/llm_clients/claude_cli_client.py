"""Claude through the Claude Code CLI, so a run uses the person's Claude plan.

Each model call runs ``claude -p`` once, headless and with every built-in tool
off, and reads its JSON result. The CLI signs in on its own (``claude auth``),
with a Claude Pro or Max plan or whatever account it is set up for, so no API
key reaches TradingAgents.

The CLI takes one prompt per call, so the conversation is written out as a
transcript. Tool calls and structured output both go through the CLI's
``--json-schema``: a tool turn answers with ``{"content", "tool_calls"}``, which
is turned back into an ``AIMessage`` with tool calls for the graph to run.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel

from .base_client import BaseLLMClient
from .validators import validate_model

logger = logging.getLogger(__name__)

CLI_PATH_ENV = "TRADINGAGENTS_CLAUDE_CLI"
INSTALL_HINT = (
    "The Claude Code CLI was not found. Install it (https://claude.com/claude-code), "
    "sign in once with `claude`, or point TRADINGAGENTS_CLAUDE_CLI at the executable."
)
DEFAULT_TIMEOUT = 900
DEFAULT_SYSTEM = "You are a helpful assistant."

# Settings the CLI has no flag for.
_UNSUPPORTED = ("temperature", "max_tokens")

TOOLS_GUIDE = """

# Tools
These tools are available:
{listing}

You cannot run them yourself. To call one or more, list them in `tool_calls` of your
structured answer, each with its arguments, and leave `content` empty; their results come
back in the next turn as <tool_result> blocks. Once you have what you need, write your
answer in `content` and leave `tool_calls` empty."""

FORCED_GUIDE = "\nCall at least one tool now."

STRUCTURED_GUIDE = "\n\nGive your answer as the structured output; its fields hold the whole answer."


class ClaudeCLIError(RuntimeError):
    """The CLI could not be run or reported an error; the message says why."""


class ClaudeCLINotFound(ClaudeCLIError):
    """No Claude Code executable could be found; retrying will not help."""


# --- finding the executable ---------------------------------------------------------


def _version_key(path: Path) -> tuple[int, ...]:
    """The version in an extension folder name such as ``anthropic.claude-code-2.1.310-win32-x64``."""
    match = re.search(r"claude-code-(\d+(?:\.\d+)*)", str(path))
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def find_claude_cli() -> str | None:
    """The Claude Code executable: TRADINGAGENTS_CLAUDE_CLI, then PATH, then known installs.

    The editor extensions ship their own copy of the CLI, which is used when no
    standalone install is on PATH; the newest one wins.
    """
    configured = os.environ.get(CLI_PATH_ENV)
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    home = Path.home()
    names = ("claude.exe", "claude") if os.name == "nt" else ("claude",)
    for candidate in (home / ".local" / "bin", home / ".claude" / "local"):
        for name in names:
            if (candidate / name).is_file():
                return str(candidate / name)
    bundled = [
        binary
        for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf")
        for binary in (home / editor / "extensions").glob("anthropic.claude-code-*/resources/native-binary/claude*")
        if binary.name in names
    ]
    if bundled:
        return str(max(bundled, key=_version_key))
    return None


# --- the transcript -----------------------------------------------------------------


def _text(content: Any) -> str:
    """A message's content as plain text, keeping only text blocks of a block list."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(part for part in parts if part)


def render_messages(messages: list[BaseMessage]) -> tuple[str, str]:
    """The system prompt and the user prompt that stand for ``messages``.

    A single user message is sent as it is; a longer conversation is written out
    as a transcript, tool calls and their results included.
    """
    system = "\n\n".join(_text(m.content) for m in messages if isinstance(m, SystemMessage))
    turns = [m for m in messages if not isinstance(m, SystemMessage)]
    if len(turns) == 1 and isinstance(turns[0], HumanMessage):
        return system, _text(turns[0].content)

    written = []
    for message in turns:
        if isinstance(message, AIMessage):
            lines = [_text(message.content)]
            for call in message.tool_calls:
                args = json.dumps(call["args"], default=str)
                lines.append(f'[tool call {call["name"]} id={call.get("id")}] {args}')
            written.append("<assistant>\n" + "\n".join(line for line in lines if line) + "\n</assistant>")
        elif isinstance(message, ToolMessage):
            written.append(f'<tool_result name="{message.name or "tool"}" id="{message.tool_call_id}">\n'
                           f"{_text(message.content)}\n</tool_result>")
        else:
            written.append(f"<user>\n{_text(message.content)}\n</user>")
    prompt = ("The conversation so far follows. Write the assistant's next turn.\n\n"
              + "\n\n".join(written))
    return system, prompt


def tool_turn_schema(tools: list[dict], required: str | None = None) -> dict:
    """The JSON schema of a turn that may call ``tools``.

    ``required`` is ``"any"`` when the turn must call a tool, or the name of the
    one tool it must call.
    """
    offered = [t for t in tools if required in (None, "any") or t["name"] == required]
    calls = [
        {
            "type": "object",
            "properties": {
                "name": {"const": tool["name"]},
                "args": tool.get("parameters") or {"type": "object", "properties": {}},
            },
            "required": ["name", "args"],
        }
        for tool in offered
    ]
    tool_calls: dict[str, Any] = {"type": "array", "items": {"anyOf": calls}}
    if required is not None:
        tool_calls["minItems"] = 1
    return {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Your answer; empty while you call tools."},
            "tool_calls": tool_calls,
        },
        "required": ["content", "tool_calls"],
    }


def _required_tool(tool_choice: Any) -> str | None:
    """``tool_choice`` as ``tool_turn_schema`` takes it."""
    if tool_choice in (None, "auto", "none", False):
        return None
    if tool_choice in ("any", "required", True):
        return "any"
    if isinstance(tool_choice, dict):
        return (tool_choice.get("function") or tool_choice).get("name") or "any"
    return str(tool_choice)


def _usage(result: dict) -> dict | None:
    """The run's token counts as LangChain usage metadata."""
    usage = result.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = sum(int(usage.get(k) or 0) for k in
                       ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    output_tokens = int(usage.get("output_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens}


# --- the chat model -----------------------------------------------------------------


class ChatClaudeCLI(BaseChatModel):
    """A LangChain chat model that answers through ``claude -p``."""

    model: str = "sonnet"
    effort: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = 2
    cli_path: str | None = None

    @property
    def _llm_type(self) -> str:
        return "claude-cli"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": self.model, "effort": self.effort}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        formatted = [convert_to_openai_tool(tool)["function"] for tool in tools]
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def with_structured_output(self, schema, *, include_raw: bool = False, **kwargs):
        if include_raw:
            raise NotImplementedError("claude_cli structured output does not return the raw message")
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            json_schema = schema.model_json_schema()

            def parse(message):
                return schema.model_validate_json(message.content)
        else:
            json_schema = convert_to_openai_tool(schema)["function"]["parameters"]

            def parse(message):
                return json.loads(message.content)
        return self.bind(response_schema=json_schema) | RunnableLambda(parse)

    def _generate(self, messages, stop=None, run_manager=None, *, tools=None, tool_choice=None,
                  response_schema=None, **kwargs) -> ChatResult:
        system, prompt = render_messages(messages)
        system = system or DEFAULT_SYSTEM
        schema = None
        if response_schema is not None:
            schema = response_schema
            system += STRUCTURED_GUIDE
        elif tools and tool_choice != "none":
            required = _required_tool(tool_choice)
            schema = tool_turn_schema(tools, required)
            listing = "\n".join(f"- {t['name']}: {t.get('description', '').strip()}" for t in tools)
            system += TOOLS_GUIDE.format(listing=listing) + (FORCED_GUIDE if required else "")

        result = self._run(system, prompt, schema)
        message = self._message(result, schema, tools if response_schema is None else None)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _message(self, result: dict, schema: dict | None, tools: list | None) -> AIMessage:
        metadata = {"model_name": self.model, "session_id": result.get("session_id"),
                    "total_cost_usd": result.get("total_cost_usd")}
        usage = _usage(result)
        if schema is None:
            return AIMessage(content=result.get("result") or "", response_metadata=metadata,
                             usage_metadata=usage)
        answer = result.get("structured_output")
        if answer is None:
            try:
                answer = json.loads(result.get("result") or "")
            except ValueError as exc:
                raise ClaudeCLIError("The Claude CLI returned no structured output") from exc
        if not tools:
            return AIMessage(content=json.dumps(answer), response_metadata=metadata, usage_metadata=usage)
        calls = [
            {"name": call["name"], "args": call.get("args") or {},
             "id": f"call_{uuid.uuid4().hex[:24]}", "type": "tool_call"}
            for call in answer.get("tool_calls") or []
        ]
        return AIMessage(content=answer.get("content") or "", tool_calls=calls,
                         response_metadata=metadata, usage_metadata=usage)

    def _command(self, cli: str, system_file: str, schema: dict | None) -> list[str]:
        command = [
            cli, "-p", "--output-format", "json", "--model", self.model,
            "--system-prompt-file", system_file,
            # A model, not an agent: no built-in tools, MCP servers, skills or saved session.
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence",
        ]
        if self.effort:
            command += ["--effort", self.effort]
        if schema is not None:
            command += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        return command

    def _run(self, system: str, prompt: str, schema: dict | None) -> dict:
        """Run the CLI once (retrying failures ``max_retries`` times) and return its JSON result."""
        cli = self.cli_path or find_claude_cli()
        if not cli:
            raise ClaudeCLINotFound(INSTALL_HINT)
        attempt = 0
        while True:
            try:
                return self._run_once(cli, system, prompt, schema)
            except ClaudeCLINotFound:
                raise
            except ClaudeCLIError as exc:
                if attempt >= self.max_retries:
                    raise
                attempt += 1
                logger.warning("Claude CLI call failed (%s); retry %d of %d", exc, attempt, self.max_retries)
                time.sleep(min(2 ** attempt, 30))

    def _run_once(self, cli: str, system: str, prompt: str, schema: dict | None) -> dict:
        # A working directory of its own, so no project's CLAUDE.md or settings reach the model.
        with tempfile.TemporaryDirectory(prefix="tradingagents-claude-") as workdir:
            system_file = os.path.join(workdir, "system.md")
            Path(system_file).write_text(system, encoding="utf-8")
            try:
                done = subprocess.run(
                    self._command(cli, system_file, schema), input=prompt, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=self.timeout, cwd=workdir,
                )
            except FileNotFoundError as exc:
                raise ClaudeCLINotFound(f"{INSTALL_HINT} ({exc})") from exc
            except subprocess.TimeoutExpired as exc:
                raise ClaudeCLIError(f"The Claude CLI did not answer within {self.timeout:.0f}s") from exc
        result = _last_json(done.stdout)
        if result is None or done.returncode != 0 or result.get("is_error"):
            detail = (result or {}).get("result") or done.stderr.strip() or done.stdout.strip()
            raise ClaudeCLIError(f"The Claude CLI failed (exit {done.returncode}): {detail[:500]}")
        return result


def _last_json(stdout: str) -> dict | None:
    """The CLI's JSON result: the whole output, else its last line that parses."""
    for text in [stdout, *reversed(stdout.strip().splitlines())]:
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class ClaudeCLIClient(BaseLLMClient):
    """Client for Claude through the Claude Code CLI (the ``claude_cli`` provider)."""

    provider = "claude_cli"

    def get_llm(self) -> Any:
        self.warn_if_unknown_model()
        for name in _UNSUPPORTED:
            if self.kwargs.get(name) is not None:
                logger.warning("The Claude CLI does not take %s; it is not sent.", name)
        llm_kwargs: dict[str, Any] = {"model": self.model}
        if self.kwargs.get("effort"):
            llm_kwargs["effort"] = self.kwargs["effort"]
        for key in ("timeout", "max_retries", "callbacks"):
            if self.kwargs.get(key) is not None:
                llm_kwargs[key] = self.kwargs[key]
        return ChatClaudeCLI(**llm_kwargs)

    def validate_model(self) -> bool:
        return validate_model("claude_cli", self.model)
