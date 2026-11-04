"""Metriky agenta ve formátu Prometheus (text exposition 0.0.4).

Prometheus objeví agenty přes HTTP service discovery backendu CMU
(GET /api/v1/integrations/prometheus/targets) a tento endpoint stahuje.
Labely `vm` a `environment` dodává service discovery, tady se neopakují.
"""

from __future__ import annotations

from collections.abc import Iterable

from .manager import ChainManager
from .models import ChainState, InstanceState


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    return "{" + ",".join(f'{k}="{_esc(str(v))}"' for k, v in labels.items()) + "}"


class _Family:
    def __init__(self, name: str, kind: str, help_: str) -> None:
        self.name, self.kind, self.help = name, kind, help_
        self.samples: list[tuple[str, float]] = []

    def add(self, value: float, **labels: str) -> None:
        self.samples.append((_labels(**labels) if labels else "", value))

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} {self.kind}"
        for lbl, value in self.samples:
            yield f"{self.name}{lbl} {value:g}"


def render(manager: ChainManager, version: str) -> str:
    info = _Family("cmu_agent_info", "gauge", "Verze agenta")
    info.add(1, version=version)

    chain_state = _Family("cmu_chain_state", "gauge", "Stav řetězce (1 = aktuální stav)")
    chain_desired = _Family("cmu_chain_desired_running", "gauge", "Řetězec má běžet")
    app_state = _Family("cmu_app_state", "gauge", "Stav instance aplikace (1 = aktuální stav)")
    app_ready = _Family("cmu_app_ready", "gauge", "Instance odpovídá 200 na /health/ready")
    app_restarts = _Family("cmu_app_restarts_total", "counter", "Počet restartů instance od startu agenta")
    app_pending = _Family("cmu_app_config_pending", "gauge", "Konfigurace změněna od startu instance")

    for chain_id, rt in manager.chains.items():
        current = manager.chain_state(rt)
        for s in ChainState:
            chain_state.add(1 if s == current else 0, chain=chain_id, name=rt.spec.name, state=s.value)
        chain_desired.add(1 if rt.desired_running else 0, chain=chain_id)
        for a in rt.apps:
            lbl = {"chain": chain_id, "app": a.l.spec.type, "version": a.l.spec.version}
            for st in InstanceState:
                app_state.add(1 if a.state == st else 0, **lbl, state=st.value)
            app_ready.add(1 if a.ready else 0, **lbl)
            app_restarts.add(a.restarts, **lbl)
            app_pending.add(1 if a.config_pending else 0, **lbl)

    families = [info, chain_state, chain_desired, app_state, app_ready, app_restarts, app_pending]
    return "\n".join(line for f in families for line in f.render()) + "\n"
