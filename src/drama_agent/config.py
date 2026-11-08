"""Runtime configuration from environment variables (optionally loaded from a .env file)."""

from __future__ import annotations

import os
from pathlib import Path

from .llm import FixtureModelClient, ModelClient
from .store import InMemoryRepository, Repository


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader: KEY=VALUE lines, optional quotes. Existing env vars win."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def client_from_env() -> ModelClient:
    """DRAMA_MODEL_CLIENT selects the backend: 'fixture' (default) or 'mcphub'.

    Opt-in on purpose: having MCPHUB_KEY in the environment alone never triggers paid calls.
    """
    kind = os.environ.get("DRAMA_MODEL_CLIENT", "fixture").lower()
    if kind == "fixture":
        return FixtureModelClient()
    if kind == "mcphub":
        from .llm.mcphub import MCPHubClient

        missing = [k for k in ("MCPHUB_URL", "MCPHUB_KEY", "MCPHUB_MODEL") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"DRAMA_MODEL_CLIENT=mcphub but missing env vars: {missing}")
        return MCPHubClient(
            base_url=os.environ["MCPHUB_URL"],
            api_key=os.environ["MCPHUB_KEY"],
            model=os.environ["MCPHUB_MODEL"],
            stage_models=stage_models_from_env(),
            timeout_s=float(os.environ.get("MCPHUB_TIMEOUT_S", "600")),
        )
    raise RuntimeError(f"unknown DRAMA_MODEL_CLIENT={kind!r}")


STAGE_MODEL_PREFIX = "MCPHUB_MODEL_"


def stage_models_from_env() -> dict[str, str]:
    """Per-step model overrides: MCPHUB_MODEL_INGEST, _OUTLINE, _SCRIPT, _STORYBOARD,
    _REVIEW. Steps without one use MCPHUB_MODEL. A misspelt variable is an error rather
    than a silently ignored setting."""
    from .models.package import STAGES

    known = {f"{STAGE_MODEL_PREFIX}{s.upper()}": s for s in STAGES}
    overrides: dict[str, str] = {}
    for key, value in os.environ.items():
        if not key.startswith(STAGE_MODEL_PREFIX):
            continue
        if key not in known:
            raise RuntimeError(
                f"unknown setting {key}; per-step models are {', '.join(sorted(known))}"
            )
        if value.strip():
            overrides[known[key]] = value.strip()
    return overrides


def repo_from_env() -> Repository:
    """DRAMA_STORE selects storage: 'sqlite' (default) or 'memory'.

    DRAMA_DB_PATH sets the SQLite file (default data/drama.db, relative to the working dir).
    """
    kind = os.environ.get("DRAMA_STORE", "sqlite").lower()
    if kind == "memory":
        return InMemoryRepository()
    if kind == "sqlite":
        from .store.sqlite import SqliteRepository

        return SqliteRepository(os.environ.get("DRAMA_DB_PATH", "data/drama.db"))
    raise RuntimeError(f"unknown DRAMA_STORE={kind!r}")


def worker_settings() -> dict[str, float | int]:
    """DRAMA_WORKERS: in-process workers for the API (default 1; 0 = run workers separately).
    DRAMA_JOB_MAX_ATTEMPTS (default 3) and DRAMA_JOB_LEASE_S (default 30) tune the queue."""
    return {
        "workers": int(os.environ.get("DRAMA_WORKERS", "1")),
        "max_attempts": int(os.environ.get("DRAMA_JOB_MAX_ATTEMPTS", "3")),
        "lease_s": float(os.environ.get("DRAMA_JOB_LEASE_S", "30")),
    }


def queue_from_env(repo: Repository):  # -> JobQueue (import kept lazy)
    from .jobqueue import InMemoryJobQueue, SqliteJobQueue
    from .store.sqlite import SqliteRepository

    max_attempts = int(worker_settings()["max_attempts"])
    if isinstance(repo, SqliteRepository):
        return SqliteJobQueue(repo, max_attempts=max_attempts)
    return InMemoryJobQueue(max_attempts=max_attempts)
