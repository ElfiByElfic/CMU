"""Jednotný model, který backend vystavuje frontendu (a dalším klientům).

Řetězec je vždy jedna dlaždice. Kde běží, říká `location` (VM s agentem, nebo server
v Komodu). Ze kterého systému pochází, říká `source`.

ID jsou složená a stabilní:
    řetězec   <sourceId>:<location>:<lokální id>    např. agents:vm-prod-01:chain-a
    lokalita  <sourceId>:<location>                 např. komodo:docker-01
    operace   <sourceId>:<location>:<id operace>    (nebo local:<id> pro operace backendu)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ChainKind(str, Enum):
    process = "process"      # procesy spravované chain-agentem na VM
    container = "container"  # kontejnery (docker compose stack v Komodu)


class ChainState(str, Enum):
    running = "running"
    degraded = "degraded"
    broken = "broken"
    starting = "starting"
    stopping = "stopping"
    stopped = "stopped"
    unknown = "unknown"


class Health(str, Enum):
    """Souhrnný stav pro dlaždice. Pořadí = závažnost (řazení „nejdřív problémy“)."""

    problem = "problem"
    warning = "warning"
    unknown = "unknown"
    stopped = "stopped"
    ok = "ok"


HEALTH_ORDER = {h: i for i, h in enumerate(Health)}


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    average = "average"
    high = "high"
    disaster = "disaster"


class SourceRef(BaseModel):
    id: str
    type: str


class LocationRef(BaseModel):
    id: str
    name: str
    address: str | None = None
    environment: str | None = None
    kind: Literal["vm", "docker-host", "demo"] = "vm"


class Alert(BaseModel):
    source: str = Field(description="Integrace, ze které výstraha pochází (zabbix, …)")
    severity: Severity
    name: str
    host: str | None = None
    since: datetime | None = None
    url: str | None = None


class Metric(BaseModel):
    id: str
    label: str
    value: float | None
    unit: str | None = None
    source: str = "prometheus"


class ChainApp(BaseModel):
    id: str
    name: str
    position: int
    state: str = Field(description="Stav instance: running, degraded, starting, stopped, error, unreachable, …")
    ready: bool | None = None
    version: str | None = None
    image: str | None = None
    port: int | None = None
    restarts: int | None = None
    configPending: bool | None = None
    detail: dict[str, Any] = Field(default_factory=dict, description="Doplňující údaje zdroje")


class Chain(BaseModel):
    id: str
    name: str
    kind: ChainKind
    source: SourceRef
    location: LocationRef
    state: ChainState
    health: Health = Health.unknown
    desiredRunning: bool | None = None
    stale: bool = Field(default=False, description="Data jsou zastaralá (zdroj neodpovídá), stavy neplatí")
    lastSeen: datetime | None = None
    apps: list[ChainApp] = []
    alerts: list[Alert] = []
    metrics: list[Metric] = []
    capabilities: list[str] = Field(default_factory=list, description="Akce, které zdroj umí")
    busyOperation: str | None = None
    localId: str = Field(description="ID řetězce ve zdroji")


class Location(BaseModel):
    id: str
    name: str
    kind: Literal["vm", "docker-host", "demo"]
    source: SourceRef
    address: str | None = None
    environment: str | None = None
    reachable: bool
    lastSeen: datetime | None = None
    error: str | None = None
    alerts: list[Alert] = []
    chainCount: int = 0


class ComponentStatus(BaseModel):
    """Stav zdroje nebo integrace (pro stránku Zdroje a pro monitoring backendu)."""

    id: str
    type: str
    role: Literal["source", "integration"]
    ok: bool
    message: str = ""
    lastSuccess: datetime | None = None
    lastAttempt: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OperationStep(BaseModel):
    target: str
    status: Literal["pending", "running", "succeeded", "failed"]
    message: str = ""


class Operation(BaseModel):
    id: str
    kind: str
    target: str
    status: Literal["running", "succeeded", "failed"]
    message: str = ""
    steps: list[OperationStep] = []
    startedAt: datetime | None = None
    finishedAt: datetime | None = None


# ---------------------------------------------------------------- vytváření řetězců

class Target(BaseModel):
    """Kam lze nový řetězec umístit."""

    sourceId: str
    sourceType: str
    kind: ChainKind
    location: str
    locationId: str
    environment: str | None = None
    reachable: bool


class CatalogEntry(BaseModel):
    type: str
    versions: list[str]


class ProcessAppSpec(BaseModel):
    type: str = Field(min_length=1)
    version: str = Field(min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class ContainerAppSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$", description="Název služby v compose")
    image: str = Field(min_length=1)
    port: int = Field(default=8080, ge=1, le=65535, description="Port povinného API uvnitř kontejneru")
    env: dict[str, str] = Field(default_factory=dict)
    publish: int | None = Field(default=None, ge=1, le=65535, description="Port na hostiteli (volitelné)")
    healthcheck: bool = Field(default=True, description="Healthcheck přes /api/v1/health/ready (curl v image)")


class ChainCreate(BaseModel):
    sourceId: str
    location: str
    kind: ChainKind
    name: str = Field(min_length=1, max_length=80)
    start: bool = True
    processApps: list[ProcessAppSpec] = []
    containerApps: list[ContainerAppSpec] = []


class ChainValidation(BaseModel):
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    preview: str | None = Field(default=None, description="Náhled výsledku (např. docker-compose.yaml)")
    assignedPorts: list[int] = []


class Summary(BaseModel):
    chains: int
    byHealth: dict[str, int]
    byKind: dict[str, int]
    locations: int
    unreachableLocations: int
    generatedAt: datetime
