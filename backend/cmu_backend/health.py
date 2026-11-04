"""Výpočet souhrnného stavu (health) dlaždice ze stavu řetězce a výstrah integrací."""

from __future__ import annotations

from .model import Chain, ChainState, Health, Severity

_STATE_HEALTH = {
    ChainState.running: Health.ok,
    ChainState.degraded: Health.warning,
    ChainState.starting: Health.warning,
    ChainState.stopping: Health.warning,
    ChainState.broken: Health.problem,
    ChainState.unknown: Health.unknown,
}

_ALERT_HEALTH = {
    Severity.disaster: Health.problem,
    Severity.high: Health.problem,
    Severity.average: Health.warning,
    Severity.warning: Health.warning,
}


def worse(a: Health, b: Health) -> Health:
    """Závažnější ze dvou; „stopped“ a „ok“ výstraha vždy přebije."""
    order = [Health.problem, Health.warning, Health.unknown, Health.stopped, Health.ok]
    return a if order.index(a) <= order.index(b) else b


def compute(chain: Chain) -> Health:
    if chain.stale or chain.state == ChainState.unknown:
        return Health.unknown  # nikdy neukazovat poslední známý stav jako aktuální
    if chain.state == ChainState.stopped:
        h = Health.problem if chain.desiredRunning else Health.stopped
    else:
        h = _STATE_HEALTH[chain.state]
    if h == Health.ok and any(a.configPending for a in chain.apps):
        h = Health.warning
    for alert in chain.alerts:
        h = worse(h, _ALERT_HEALTH.get(alert.severity, Health.ok))
    return h
