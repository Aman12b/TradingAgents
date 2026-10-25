"""The run settings the UI starts from, the choices it offers, and whether a provider is ready."""

import os

from cli.prompts import _llm_provider_table, resolve_backend_url
from tradingagents.default_config import build_default_config
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

ANALYSTS = [
    ("market", "Market Analyst"),
    ("social", "Sentiment Analyst"),
    ("news", "News Analyst"),
    ("fundamentals", "Fundamentals Analyst"),
]

DEPTHS = [(1, "Shallow (1 Runde)"), (3, "Medium (3 Runden)"), (5, "Deep (5 Runden)")]

LANGUAGES = ["English", "German", "Chinese", "Japanese", "Korean", "Hindi", "Spanish",
             "Portuguese", "French", "Russian", "Arabic"]

# The thinking setting each provider takes, its values, and its config key.
THINKING = {
    "anthropic": ("anthropic_effort", ["", "low", "medium", "high", "xhigh", "max"]),
    "claude_cli": ("anthropic_effort", ["", "low", "medium", "high", "xhigh", "max"]),
    "openai": ("openai_reasoning_effort", ["", "low", "medium", "high"]),
    "chatgpt": ("openai_reasoning_effort", ["", "low", "medium", "high"]),
    "google": ("google_thinking_level", ["", "minimal", "low", "medium", "high"]),
}

# What a run takes from the settings; every field of a job's settings is one of these.
RUN_KEYS = (
    "llm_provider", "quick_think_llm", "deep_think_llm", "backend_url",
    "anthropic_effort", "openai_reasoning_effort", "google_thinking_level",
    "output_language", "research_depth", "analysts", "max_tool_rounds", "checkpoint_enabled",
)


def builtin_defaults() -> dict:
    """The defaults from the environment and DEFAULT_CONFIG, for a first start."""
    config = build_default_config()
    return {
        "llm_provider": config["llm_provider"],
        "quick_think_llm": config["quick_think_llm"],
        "deep_think_llm": config["deep_think_llm"],
        "backend_url": config.get("backend_url") or "",
        "anthropic_effort": config.get("anthropic_effort") or "",
        "openai_reasoning_effort": config.get("openai_reasoning_effort") or "",
        "google_thinking_level": config.get("google_thinking_level") or "",
        "output_language": config.get("output_language") or "English",
        "research_depth": int(config.get("max_debate_rounds") or 1),
        "analysts": [key for key, _ in ANALYSTS],
        "max_tool_rounds": int(config.get("max_tool_rounds") or 20),
        # A run that stops halfway resumes from its last step when retried.
        "checkpoint_enabled": True,
    }


def clean_settings(values: dict, base: dict) -> dict:
    """``values`` over ``base``, keeping only run settings and coercing their types."""
    merged = {**base, **{k: v for k, v in (values or {}).items() if k in RUN_KEYS}}
    merged["research_depth"] = max(1, min(10, int(merged.get("research_depth") or 1)))
    merged["max_tool_rounds"] = max(1, min(45, int(merged.get("max_tool_rounds") or 20)))
    merged["checkpoint_enabled"] = bool(merged.get("checkpoint_enabled"))
    analysts = [a for a in merged.get("analysts") or [] if a in dict(ANALYSTS)]
    merged["analysts"] = analysts or [key for key, _ in ANALYSTS]
    for key in ("llm_provider", "quick_think_llm", "deep_think_llm"):
        merged[key] = str(merged.get(key) or "").strip()
    merged["llm_provider"] = merged["llm_provider"].lower()
    return merged


def run_config(settings: dict) -> dict:
    """The TradingAgents config for a run with ``settings``."""
    config = build_default_config()
    provider = settings["llm_provider"]
    for key in ("llm_provider", "quick_think_llm", "deep_think_llm", "output_language",
                "max_tool_rounds", "checkpoint_enabled"):
        config[key] = settings[key]
    config["backend_url"] = resolve_backend_url(provider, None, settings.get("backend_url") or None)
    config["max_debate_rounds"] = settings["research_depth"]
    config["max_risk_discuss_rounds"] = settings["research_depth"]
    for key in ("anthropic_effort", "openai_reasoning_effort", "google_thinking_level"):
        config[key] = settings.get(key) or None
    return config


def provider_problem(provider: str) -> str | None:
    """Why a run on ``provider`` cannot start now, or None when it can."""
    provider = provider.lower()
    if provider == "claude_cli":
        from tradingagents.llm_clients.claude_cli_client import INSTALL_HINT, find_claude_cli

        return None if find_claude_cli() else INSTALL_HINT
    if provider == "chatgpt":
        from tradingagents.llm_clients import chatgpt_auth

        try:
            chatgpt_auth.access_token()
        except chatgpt_auth.ChatGPTAuthError as exc:
            return str(exc)
        return None
    env_var = get_api_key_env(provider)
    if env_var and provider != "openai_compatible" and not os.environ.get(env_var):
        return f"{env_var} is not set; add it to your .env file."
    return None


def provider_status(provider: str) -> dict:
    """What the settings page shows about a provider's readiness."""
    detail = None
    if provider == "claude_cli":
        from tradingagents.llm_clients.claude_cli_client import find_claude_cli

        detail = find_claude_cli()
    elif provider == "chatgpt":
        from tradingagents.llm_clients import chatgpt_auth

        detail = chatgpt_auth.signed_in_as() if chatgpt_auth.AUTH_FILE.exists() else None
    else:
        detail = get_api_key_env(provider)
    # Only the sign-in needs a network round trip; the rest is a local check.
    problem = provider_problem(provider) if provider != "chatgpt" else (
        None if detail else "Not signed in; run `tradingagents login chatgpt`.")
    return {"ready": problem is None, "problem": problem, "detail": detail}


def options() -> dict:
    """Every choice the forms offer."""
    providers = []
    for display, key, _url in _llm_provider_table():
        models = MODEL_OPTIONS.get(key, {})
        providers.append({
            "key": key,
            "name": display,
            "quick": [{"label": label, "value": value} for label, value in models.get("quick", [])
                      if value != "custom"],
            "deep": [{"label": label, "value": value} for label, value in models.get("deep", [])
                     if value != "custom"],
            "thinking": THINKING.get(key, (None, []))[0],
            "thinking_values": THINKING.get(key, (None, []))[1],
        })
    return {
        "providers": providers,
        "analysts": [{"key": k, "name": n} for k, n in ANALYSTS],
        "depths": [{"value": v, "label": label} for v, label in DEPTHS],
        "languages": LANGUAGES,
    }
