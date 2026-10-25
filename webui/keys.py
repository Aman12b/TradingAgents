"""API keys and data-source settings, read from the environment and saved to the .env file.

A key saved here goes into the same .env the CLI writes and the package loads,
and into this process's environment, so the next model or data call uses it
without a restart. The page only ever sees the last four characters of a secret.
"""

import os
from pathlib import Path

from dotenv import dotenv_values, find_dotenv, set_key, unset_key

from cli.prompts import _llm_provider_table
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

# Docker sets this to a file in the data volume, so saved keys survive a rebuilt container.
ENV_FILE_VAR = "TRADINGAGENTS_ENV_FILE"

DATA_SOURCES = [
    {"name": "FRED_API_KEY", "label": "FRED (US-Makrodaten)", "secret": True,
     "help": "Inflation, Leitzins, Arbeitslosigkeit, Zinskurve für den News-Analysten. Ohne Key schreibt er "
             "den Makro-Teil ohne echte Zahlen. Kostenlos.",
     "url": "https://fred.stlouisfed.org/docs/api/api_key.html"},
    {"name": "SEC_EDGAR_USER_AGENT", "label": "SEC EDGAR (US-Pflichtmeldungen)", "secret": False,
     "placeholder": "Vorname Nachname name@example.com",
     "help": "Kein Key, sondern Name und E-Mail-Adresse, unter denen die SEC dich erreichen kann. Bilanzen von "
             "US-Firmen kommen so direkt aus den Filings. Ohne Eintrag wird ein Platzhalter gesendet.",
     "url": "https://www.sec.gov/os/accessing-edgar-data"},
    {"name": "TYPESAFE_API_KEY", "label": "TypeSafe Jev (Social-Post-Filter)", "secret": True,
     "help": "Filtert Reddit- und StockTwits-Posts für den Sentiment-Analysten auf die, die wirklich die Firma "
             "betreffen. Ohne Key werden alle Posts ungefiltert gelesen. Kostenpflichtig; mit einem "
             "OpenRouter-Key hier und TYPESAFE_BASE_URL=https://openrouter.ai/api auch über OpenRouter."},
    {"name": "TYPESAFE_BASE_URL", "label": "TypeSafe Endpunkt (optional)", "secret": False,
     "placeholder": "https://openrouter.ai/api",
     "help": "Nur nötig, um Jev über OpenRouter statt direkt bei TypeSafe zu nutzen."},
    {"name": "ALPHA_VANTAGE_API_KEY", "label": "Alpha Vantage", "secret": True,
     "help": "Alternative Kurs-, Indikator-, Fundamental- und News-Daten. Wird nur genutzt, wenn Alpha Vantage "
             "als Datenquelle eingestellt ist; Standard ist Yahoo Finance.",
     "url": "https://www.alphavantage.co/support/#api-key"},
]

# Endpoints set next to the providers' keys; not secret.
LLM_EXTRAS = [
    {"name": "OLLAMA_BASE_URL", "label": "Ollama-Server", "secret": False,
     "placeholder": "http://localhost:11434/v1", "help": "Nur für einen Ollama-Server auf einem anderen Rechner."},
]

_REGION = {"qwen-cn": "Qwen (China)", "glm-cn": "GLM (China)", "minimax-cn": "MiniMax (China)"}


def _llm_keys() -> list[dict]:
    names = {key: display for display, key, _ in _llm_provider_table()}
    keys, seen = [], set()
    for provider, env_var in PROVIDER_API_KEY_ENV.items():
        if not env_var or env_var in seen:
            continue
        seen.add(env_var)
        label = _REGION.get(provider) or names.get(provider, provider)
        help_text = "Optional: nur für einen Endpunkt, der einen Key verlangt." \
            if provider == "openai_compatible" else f"API-Key für den Provider „{label}“."
        keys.append({"name": env_var, "label": label, "secret": True, "help": help_text})
    return keys + LLM_EXTRAS


def catalog() -> list[dict]:
    return [{"group": "LLM-Provider", "keys": _llm_keys()},
            {"group": "Datenquellen", "keys": DATA_SOURCES}]


def known() -> dict[str, dict]:
    return {k["name"]: k for group in catalog() for k in group["keys"]}


def env_file() -> Path:
    """The .env file keys are saved to: TRADINGAGENTS_ENV_FILE, else the CLI's .env."""
    configured = os.environ.get(ENV_FILE_VAR)
    if configured:
        return Path(configured)
    return Path(find_dotenv(usecwd=True) or Path.cwd() / ".env")


def _preview(value: str, secret: bool) -> str:
    if not secret:
        return value
    return "••••" + value[-4:] if len(value) > 8 else "••••"


def status() -> dict:
    """Every key: whether it is set, where from, and a preview that never shows a secret."""
    path = env_file()
    in_file = dotenv_values(path) if path.is_file() else {}
    groups = []
    for group in catalog():
        rows = []
        for key in group["keys"]:
            value = os.environ.get(key["name"]) or ""
            source = None
            if value:
                source = "file" if in_file.get(key["name"]) == value else "environment"
            rows.append({**key, "set": bool(value), "preview": _preview(value, key["secret"]) if value else "",
                         "source": source})
        groups.append({"group": group["group"], "keys": rows})
    return {"env_file": str(path), "env_file_exists": path.is_file(), "groups": groups}


def _owner_only(path: Path) -> None:
    """Create ``path`` readable by its owner only, or tighten it, as the CLI does."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    os.chmod(path, 0o600)


def save(name: str, value: str) -> None:
    """Save ``name`` to the .env file and this process; an unknown name or a bad value raises ValueError."""
    if name not in known():
        raise ValueError(f"{name} is not a setting this page manages")
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a value, or remove the key instead.")
    if any(c in value for c in "\r\n\0"):
        raise ValueError("A key is one line.")
    path = env_file()
    _owner_only(path)
    set_key(str(path), name, value)
    os.environ[name] = value


def remove(name: str) -> None:
    """Remove ``name`` from the .env file and from this process."""
    if name not in known():
        raise ValueError(f"{name} is not a setting this page manages")
    path = env_file()
    if path.is_file() and name in dotenv_values(path):
        unset_key(str(path), name)
    os.environ.pop(name, None)
