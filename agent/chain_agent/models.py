"""Datový model agenta – odpovídá schématům v api/agent-api.yaml."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InstanceState(str, Enum):
    """Stav instance pohledem agenta (AppState aplikace + stopped/unreachable)."""

    stopped = "stopped"
    starting = "starting"
    running = "running"
    degraded = "degraded"
    paused = "paused"
    draining = "draining"
    stopping = "stopping"
    error = "error"
    unreachable = "unreachable"


# Stavy hlášené aplikací v GET /api/v1/status (AppState v app-api.yaml).
APP_STATES = {
    "starting": InstanceState.starting,
    "running": InstanceState.running,
    "degraded": InstanceState.degraded,
    "paused": InstanceState.paused,
    "draining": InstanceState.draining,
    "stopping": InstanceState.stopping,
    "error": InstanceState.error,
}


class ChainState(str, Enum):
    stopped = "stopped"
    starting = "starting"
    running = "running"
    degraded = "degraded"
    broken = "broken"
    stopping = "stopping"


class AppSpec(BaseModel):
    type: str = Field(min_length=1, examples=["App2"])
    version: str = Field(min_length=1, examples=["2.4.1"])
    port: int | None = Field(default=None, description="Vynecháno = agent přidělí z rozsahu")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Konfigurace instance (vrstva řetězce). INI sekce = vnořený objekt.",
    )


class ChainSpec(BaseModel):
    id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,40}$")
    name: str
    apps: list[AppSpec] = Field(description="Pořadí = směr toku dat; první je vstup řetězce")
    restartPolicy: Literal["never", "on-failure", "always"] = "on-failure"
    autostart: bool = Field(default=True, description="Spustit po startu agenta, pokud má běžet")


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    assignedPorts: list[int] = []


class OperationStep(BaseModel):
    target: str
    status: Literal["pending", "running", "succeeded", "failed"]
    message: str = ""


class Operation(BaseModel):
    id: str
    kind: str
    target: str
    status: Literal["running", "succeeded", "failed"] = "running"
    message: str = ""
    steps: list[OperationStep] = []
    startedAt: datetime = Field(default_factory=utcnow)
    finishedAt: datetime | None = None
