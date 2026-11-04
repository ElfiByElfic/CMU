"""Konfigurace backendu (YAML). Řetězce ${PROMĚNNÁ} se nahradí proměnnými prostředí,
takže tokeny a hesla nemusí být v souboru."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            raise ValueError(f"proměnná prostředí {name} není nastavená")
        return _ENV.sub(repl, value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


class AuthSettings(BaseModel):
    """Prázdné seznamy = bez přihlášení (jen pro vývoj)."""

    viewerTokens: list[str] = []
    operatorTokens: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.viewerTokens or self.operatorTokens)


class AgentEntry(BaseModel):
    name: str = Field(min_length=1, description="Jméno VM – musí odpovídat vmName agenta")
    address: str
    port: int | None = None
    environment: str | None = None
    token: str | None = None
    controlToken: str | None = None


class AgentSourceSettings(BaseModel):
    type: Literal["agent"]
    id: str = "agents"
    port: int = 9100
    scheme: Literal["http", "https"] = "http"
    token: str = Field(default="", description="Token role read na agentech (výchozí pro všechny)")
    controlToken: str = Field(default="", description="Token role control na agentech (pro akce)")
    agents: list[AgentEntry] = []
    inventoryFile: Path | None = Field(default=None, description="YAML se seznamem agentů (stejný tvar jako agents)")
    concurrency: int = Field(default=64, ge=1, le=1024)
    timeoutS: float = Field(default=5.0, gt=0)
    verifyTls: bool = True


class KomodoSourceSettings(BaseModel):
    type: Literal["komodo"]
    id: str = "komodo"
    url: str
    apiKey: str
    apiSecret: str
    stackPrefix: str = Field(default="cmu-", description="Stacky s tímto prefixem jsou řetězce CMU")
    environment: str | None = None
    concurrency: int = Field(default=16, ge=1, le=256)
    timeoutS: float = Field(default=10.0, gt=0)
    verifyTls: bool = True


class DemoSourceSettings(BaseModel):
    type: Literal["demo"]
    id: str = "demo"
    seed: int = 7
    stepS: float = Field(default=1.0, gt=0, description="Délka simulovaného kroku akce")


SourceSettings = Annotated[
    AgentSourceSettings | KomodoSourceSettings | DemoSourceSettings, Field(discriminator="type")
]


class ZabbixSettings(BaseModel):
    url: str = Field(description="Celá adresa API, např. https://zabbix.example/api_jsonrpc.php")
    token: str
    hostField: Literal["host", "name"] = "host"
    hostMap: dict[str, str] = Field(default_factory=dict, description="Lokalita CMU → host v Zabbixu")
    minSeverity: int = Field(default=2, ge=0, le=5, description="0 nezařazeno … 5 katastrofa")
    intervalS: float = Field(default=30, gt=0)
    timeoutS: float = Field(default=10, gt=0)
    webUrl: str | None = Field(default=None, description="Adresa webu Zabbixu pro odkazy u výstrah")
    verifyTls: bool = True


class PrometheusMetric(BaseModel):
    id: str
    label: str
    query: str = Field(description="Instant dotaz PromQL vracející vektor s labely řetězce")
    unit: str | None = None
    chainLabel: str = "chain"
    locationLabel: str = "vm"


class PrometheusSettings(BaseModel):
    url: str | None = Field(default=None, description="Adresa Prometheu pro čtení metrik (nepovinné)")
    token: str | None = None
    intervalS: float = Field(default=30, gt=0)
    timeoutS: float = Field(default=10, gt=0)
    metrics: list[PrometheusMetric] = []
    agentMetricsPath: str = "/agent/v1/metrics"
    verifyTls: bool = True


class IntegrationsSettings(BaseModel):
    zabbix: ZabbixSettings | None = None
    prometheus: PrometheusSettings | None = None


class Settings(BaseModel):
    listenAddress: str = "0.0.0.0"
    listenPort: int = 8000
    pollIntervalS: float = Field(default=5, gt=0)
    staleAfterS: float | None = Field(default=None, description="Výchozí 3× pollIntervalS")
    auth: AuthSettings = AuthSettings()
    sources: list[SourceSettings] = []
    integrations: IntegrationsSettings = IntegrationsSettings()
    staticDir: Path | None = Field(default=None, description="Sestavený frontend (frontend/dist) pro /")
    corsOrigins: list[str] = []

    @property
    def stale_after(self) -> float:
        return self.staleAfterS or 3 * self.pollIntervalS

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.model_validate(expand_env(raw))
