"""Integrace Prometheus – oba směry:

1. **Service discovery**: backend zná všechny agenty, takže Prometheu poskytuje jejich seznam
   (`http_sd_configs`, GET /api/v1/integrations/prometheus/targets). Agent vystavuje
   /agent/v1/metrics. Přidání VM do inventáře = automaticky monitorovaná.
2. **Metriky v dlaždicích**: instant dotazy PromQL z konfigurace. Každý dotaz vrací vektor
   s labely řetězce a lokality (výchozí `chain`, `vm`), jeden dotaz na metriku za interval.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..model import Chain, Location, Metric, utcnow
from ..settings import PrometheusSettings
from .base import Integration

log = logging.getLogger(__name__)


def sd_targets(agent_sources: list[Any], cfg: PrometheusSettings | None) -> list[dict[str, Any]]:
    """Formát Prometheus HTTP SD: [{"targets": ["host:port"], "labels": {...}}]."""
    path = cfg.agentMetricsPath if cfg else "/agent/v1/metrics"
    out = []
    for src in agent_sources:
        for entry, environment in src.agent_targets():
            labels = {"__metrics_path__": path, "vm": entry.name, "cmu_source": src.id}
            if environment:
                labels["environment"] = environment
            out.append({"targets": [f"{entry.address}:{entry.port or src.default_port}"], "labels": labels})
    return out


class PrometheusIntegration(Integration):
    id = "prometheus"

    def __init__(self, cfg: PrometheusSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.interval_s = cfg.intervalS
        headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
        self._client = httpx.AsyncClient(base_url=(cfg.url or "").rstrip("/"), timeout=cfg.timeoutS,
                                         verify=cfg.verifyTls, headers=headers, transport=transport)
        self._values: dict[tuple[str, str], list[Metric]] = {}  # (lokalita, řetězec) → metriky

    async def refresh(self, chains: list[Chain], locations: list[Location]) -> None:
        if not self.cfg.url or not self.cfg.metrics:
            return
        self.last_attempt = utcnow()
        values: dict[tuple[str, str], list[Metric]] = {}
        errors = []
        for m in self.cfg.metrics:
            try:
                r = await self._client.get("/api/v1/query", params={"query": m.query})
                data = r.json()
                if r.status_code != 200 or data.get("status") != "success":
                    raise ValueError(data.get("error") or f"HTTP {r.status_code}")
                result = data["data"]["result"]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                errors.append(f"{m.id}: {e}")
                continue
            for sample in result:
                labels = sample.get("metric", {})
                key = (labels.get(m.locationLabel, ""), labels.get(m.chainLabel, ""))
                try:
                    value: float | None = float(sample["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    value = None
                if value is not None and value != value:  # NaN
                    value = None
                values.setdefault(key, []).append(Metric(id=m.id, label=m.label, value=value, unit=m.unit))
        if errors and len(errors) == len(self.cfg.metrics):
            self.last_error = "Prometheus: " + "; ".join(errors)
            log.warning("%s", self.last_error)
            return
        self._values = values
        self.last_success = utcnow()
        self.last_error = ("Prometheus: " + "; ".join(errors)) if errors else ""

    def apply(self, chains: list[Chain], locations: list[Location]) -> None:
        if not self._values:
            return
        for ch in chains:
            if ch.stale:
                continue
            ch.metrics += self._values.get((ch.location.name, ch.localId), [])

    def status(self):  # type: ignore[no-untyped-def]
        s = super().status()
        s.details = {"url": self.cfg.url, "metrics": [m.id for m in self.cfg.metrics]}
        if not self.cfg.url:
            s.message = "jen service discovery (url pro čtení metrik není nastavené)"
        return s

    async def close(self) -> None:
        await self._client.aclose()
