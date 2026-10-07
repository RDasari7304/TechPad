"""Configuration. Reads a .env file (if present) then environment variables."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = re.sub(r"\s+#.*$", "", value).strip()  # allow trailing "# comments"
        key, value = key.strip(), value.strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Settings:
    # --- LLM ---
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    MODEL: str = os.environ.get("LARPCHECK_MODEL", "claude-sonnet-5-5")
    MOCK_LLM: bool = _bool("LARPCHECK_MOCK", False)  # run pipeline without a real LLM (for smoke tests)

    # --- Money / budget. "Allocated a certain amount of money" ---
    # Hard cap on LLM spend per test, in USD. The agent stops testing and writes a verdict
    # with what it has when this is reached.
    BUDGET_USD_PER_TEST: float = _float("LARPCHECK_BUDGET_USD", 1.50)
    # Approximate price table (USD per 1M tokens) used for the cap. Adjust if your model differs.
    PRICE_IN_PER_M: float = _float("LARPCHECK_PRICE_IN", 3.0)
    PRICE_OUT_PER_M: float = _float("LARPCHECK_PRICE_OUT", 15.0)
    # Optional Solana wallet so the agent can *try* paid products (read-only unless ALLOW_SPEND).
    SOLANA_RPC: str = os.environ.get("SOLANA_RPC", "https://api.mainnet-beta.solana.com")
    AGENT_WALLET_PUBKEY: str = os.environ.get("AGENT_WALLET_PUBKEY", "")
    ALLOW_SPEND: bool = _bool("LARPCHECK_ALLOW_SPEND", False)

    # --- Agent limits ---
    # A test is: research, one main test, and (only if that wasn't decisive) one follow-up test.
    TIME_LIMIT_SECONDS: int = _int("LARPCHECK_TIME_LIMIT_SECONDS", 60)   # whole test, wall clock, hard cap
    RESEARCH_SECONDS: int = _int("LARPCHECK_RESEARCH_SECONDS", 15)       # metadata/X/website fetch + camera tour
    CALLS_PER_TEST: int = _int("LARPCHECK_CALLS_PER_TEST", 2)            # tool calls allowed in each of the 2 tests
    HTTP_TIMEOUT: int = _int("LARPCHECK_HTTP_TIMEOUT", 10)
    USE_BROWSER: bool = _bool("LARPCHECK_USE_BROWSER", True)
    RUN_CODE: bool = _bool("LARPCHECK_RUN_CODE", True)
    # Re-use a previous verdict for the same CA if it is younger than this many hours.
    CACHE_HOURS: int = _int("LARPCHECK_CACHE_HOURS", 24)

    # --- Server / queue ---
    HOST: str = os.environ.get("LARPCHECK_HOST", "0.0.0.0")
    PORT: int = _int("LARPCHECK_PORT", 8000)
    WORKERS: int = _int("LARPCHECK_WORKERS", 6)  # concurrent agent runs; queue depth is unbounded
    DB_PATH: str = os.environ.get("LARPCHECK_DB", str(ROOT / "data" / "larpcheck.db"))
    PUBLIC_URL: str = os.environ.get("LARPCHECK_PUBLIC_URL", "http://localhost:8000")

    # --- Launchpad quick audit (one decisive test, hard caps) ---
    QUICK_BUDGET_USD: float = _float("LARPCHECK_QUICK_BUDGET_USD", 0.35)
    QUICK_MAX_TOOL_CALLS: int = _int("LARPCHECK_QUICK_MAX_TOOL_CALLS", 3)
    QUICK_MAX_SECONDS: int = _int("LARPCHECK_QUICK_MAX_SECONDS", 30)

    # --- Launchpad ---
    LAUNCH_MIN_SCORE: int = _int("LARPCHECK_LAUNCH_MIN_SCORE", 50)        # audit score needed to deploy
    # any verdict except LARP can launch if the score (probability it's real) clears the minimum
    LAUNCH_ALLOWED_VERDICTS: tuple = tuple(os.environ.get("LARPCHECK_LAUNCH_VERDICTS", "WORKS,PARTIAL,UNVERIFIED").split(","))
    PUMPPORTAL_URL: str = os.environ.get("PUMPPORTAL_URL", "https://pumpportal.fun/api/trade-local")
    PUMP_IPFS_URL: str = os.environ.get("PUMP_IPFS_URL", "https://pump.fun/api/ipfs")
    UPLOAD_DIR: str = os.environ.get("LARPCHECK_UPLOADS", str(ROOT / "data" / "uploads"))
    MAX_IMAGE_MB: int = _int("LARPCHECK_MAX_IMAGE_MB", 5)
    MAX_VIDEO_MB: int = _int("LARPCHECK_MAX_VIDEO_MB", 40)

    # --- X / Twitter bot ---
    X_BEARER_TOKEN: str = os.environ.get("X_BEARER_TOKEN", "")
    X_API_KEY: str = os.environ.get("X_API_KEY", "")
    X_API_SECRET: str = os.environ.get("X_API_SECRET", "")
    X_ACCESS_TOKEN: str = os.environ.get("X_ACCESS_TOKEN", "")
    X_ACCESS_SECRET: str = os.environ.get("X_ACCESS_SECRET", "")
    X_BOT_HANDLE: str = os.environ.get("X_BOT_HANDLE", "techpad")
    X_POLL_SECONDS: int = _int("X_POLL_SECONDS", 60)
    # Read-only tweet fetching via fxtwitter (no key needed). Set to "" to disable.
    FXTWITTER_BASE: str = os.environ.get("FXTWITTER_BASE", "https://api.fxtwitter.com")


settings = Settings()
