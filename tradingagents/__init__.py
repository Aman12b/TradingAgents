"""TradingAgents: multi-agent LLM financial trading framework."""

__version__ = "0.7.0"

import os

from dotenv import find_dotenv, load_dotenv

# Keys saved from the web UI to the file TRADINGAGENTS_ENV_FILE names (in Docker,
# one in the data volume) were set on purpose, so they win over the environment.
if os.environ.get("TRADINGAGENTS_ENV_FILE"):
    load_dotenv(os.environ["TRADINGAGENTS_ENV_FILE"], override=True)

# Load .env at package import so DEFAULT_CONFIG's env-var overlay and every LLM
# client see the user's keys whichever entry point started the process.
# usecwd=True walks from the working directory, so the installed console script
# finds the project's .env rather than looking beside site-packages. Values the
# caller has already exported are never overridden.
load_dotenv(find_dotenv(usecwd=True))
load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)
