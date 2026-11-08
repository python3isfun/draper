"""Runtime configuration from environment variables (optionally loaded from a .env file)."""

from __future__ import annotations

import os
from pathlib import Path

from .llm import FixtureModelClient, ModelClient


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
            timeout_s=float(os.environ.get("MCPHUB_TIMEOUT_S", "600")),
        )
    raise RuntimeError(f"unknown DRAMA_MODEL_CLIENT={kind!r}")
