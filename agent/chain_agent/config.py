"""Konfigurace agenta (/etc/chain-agent/agent.json)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class AgentConfig(BaseModel):
    vmName: str = Field(min_length=1)
    environment: str = "production"
    listenAddress: str = "0.0.0.0"
    listenPort: int = 9100
    readTokens: list[str] = []
    controlTokens: list[str] = Field(min_length=1)
    catalogDir: Path = Path("/opt/chain-apps")
    stateDir: Path = Path("/var/lib/chain-agent")
    logDir: Path = Path("/var/log/chain-agent")
    # Vrstva "prostředí": <envConfigDir>/<Type>.json nebo <Type>.ini (volitelné)
    envConfigDir: Path | None = None
    # Hodnoty secret://<name> se načítají ze souboru <secretsDir>/<name>
    secretsDir: Path = Path("/etc/chain-agent/secrets")
    portRange: tuple[int, int] = (8500, 8999)
    pollIntervalMs: int = Field(default=3000, ge=100)
    readyTimeoutMs: int = Field(default=60000, ge=100)
    stopTimeoutMs: int = Field(default=30000, ge=100)
    restartBackoffMaxS: int = Field(default=60, ge=1)

    @model_validator(mode="after")
    def _check(self) -> AgentConfig:
        lo, hi = self.portRange
        if not (1024 <= lo <= hi <= 65535):
            raise ValueError("portRange musí být [od, do] v rozsahu 1024–65535")
        return self

    @property
    def agent_url(self) -> str:
        """Adresa, na které aplikace na stejné VM dosáhnou agenta."""
        return f"http://127.0.0.1:{self.listenPort}"

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
