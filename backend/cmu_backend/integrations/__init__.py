"""Registr integrací. Nová integrace: třída odvozená od Integration + větev v build()."""

from __future__ import annotations

import httpx

from ..settings import Settings
from .base import Integration
from .prometheus import PrometheusIntegration, sd_targets
from .zabbix import ZabbixIntegration

__all__ = ["Integration", "PrometheusIntegration", "ZabbixIntegration", "build", "sd_targets"]


def build(settings: Settings, transports: dict[str, httpx.AsyncBaseTransport] | None = None) -> list[Integration]:
    transports = transports or {}
    out: list[Integration] = []
    if settings.integrations.zabbix:
        out.append(ZabbixIntegration(settings.integrations.zabbix, transports.get("zabbix")))
    if settings.integrations.prometheus:
        out.append(PrometheusIntegration(settings.integrations.prometheus, transports.get("prometheus")))
    return out
