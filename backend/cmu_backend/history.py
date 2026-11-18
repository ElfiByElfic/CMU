"""Sledování změn řetězců: od kdy trvá stav, poslední změna a historie událostí.

Backend po každé obnově zdroje porovná nový pohled s předchozím a zaznamená změny stavu,
souhrnného stavu (health), verzí aplikací a výstrah. Historie se ukládá do `dataDir`,
takže přežije restart backendu.

Poctivost údajů: když backend změnu stavu neviděl (řetězec už tak byl při prvním pozorování),
`stateSinceExact` je False a stav trvá *nejméně* od `stateSince`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .model import Chain, ChainEvent, utcnow
from .store import JsonStore

HEALTH_TEXT = {"problem": "Problém", "warning": "Varování", "unknown": "Nedostupné", "stopped": "Zastaveno",
               "ok": "V pořádku"}
STATE_TEXT = {"running": "běží", "degraded": "zpomalený", "broken": "přerušený", "starting": "spouští se",
              "stopping": "zastavuje se", "stopped": "zastaven", "unknown": "stav neznámý"}


def _versions(c: Chain) -> dict[str, str]:
    return {a.name: (a.version or a.image or "?") for a in c.apps}


def _alerts(c: Chain) -> set[str]:
    return {f"{a.severity.value}|{a.name}" for a in c.alerts}


@dataclass
class _Entry:
    observed_since: datetime
    state: str
    health: str
    state_since: datetime
    state_since_exact: bool
    versions: dict[str, str]
    alerts: set[str]
    last_change: datetime | None = None
    last_seen_in_view: datetime = field(default_factory=utcnow)
    events: deque[ChainEvent] = field(default_factory=deque)

    def to_json(self) -> dict[str, Any]:
        return {
            "observedSince": self.observed_since.isoformat(), "state": self.state, "health": self.health,
            "stateSince": self.state_since.isoformat(), "stateSinceExact": self.state_since_exact,
            "versions": self.versions, "alerts": sorted(self.alerts),
            "lastChange": self.last_change.isoformat() if self.last_change else None,
            "lastSeenInView": self.last_seen_in_view.isoformat(),
            "events": [e.model_dump(mode="json") for e in self.events],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any], max_events: int) -> _Entry:
        ts = datetime.fromisoformat
        return cls(
            observed_since=ts(d["observedSince"]), state=d["state"], health=d["health"],
            state_since=ts(d["stateSince"]), state_since_exact=bool(d.get("stateSinceExact")),
            versions=dict(d.get("versions", {})), alerts=set(d.get("alerts", [])),
            last_change=ts(d["lastChange"]) if d.get("lastChange") else None,
            last_seen_in_view=ts(d.get("lastSeenInView") or d["observedSince"]),
            events=deque((ChainEvent.model_validate(e) for e in d.get("events", [])), maxlen=max_events),
        )


class ChangeTracker:
    def __init__(self, store: JsonStore, max_events: int = 100, retention_days: int = 30) -> None:
        self._store = store
        self._max = max_events
        self._retention = timedelta(days=retention_days)
        raw = store.load({})
        self._entries: dict[str, _Entry] = {}
        for cid, d in raw.items():
            try:
                self._entries[cid] = _Entry.from_json(d, max_events)
            except (KeyError, ValueError, TypeError):
                continue
        self._dirty = False
        self._pending_seed: dict[str, tuple[list[ChainEvent], datetime | None, datetime | None]] = {}

    # ------------------------------------------------------------------ zápis
    def _event(self, e: _Entry, at: datetime, kind: str, text: str,
               before: str | None = None, after: str | None = None) -> None:
        e.events.append(ChainEvent(at=at, kind=kind, text=text, before=before, after=after))  # type: ignore[arg-type]
        if kind != "observed":
            e.last_change = at
        self._dirty = True

    def seed(self, chain_id: str, events: list[ChainEvent], state_since: datetime | None,
             observed_since: datetime | None) -> None:
        """Počáteční historie dodaná zdrojem (např. demo data). Použije se jen pro neznámý řetězec."""
        self._pending_seed[chain_id] = (events, state_since, observed_since)

    def observe(self, chains: list[Chain], now: datetime | None = None) -> None:
        now = now or utcnow()
        for c in chains:
            e = self._entries.get(c.id)
            state, hlth = c.state.value, c.health.value
            versions, alerts = _versions(c), _alerts(c)
            if e is None:
                seed = self._pending_seed.pop(c.id, None)
                e = _Entry(observed_since=now, state=state, health=hlth, state_since=now, state_since_exact=False,
                           versions=versions, alerts=alerts, events=deque(maxlen=self._max))
                if seed:
                    events, since, observed = seed
                    if observed:
                        # stav trvá nejméně od začátku sledování (přesně ho neznáme)
                        e.observed_since = e.state_since = observed
                    if since:
                        e.state_since, e.state_since_exact = since, True
                    for ev in sorted(events, key=lambda x: x.at):
                        e.events.append(ev)
                        if ev.kind != "observed":
                            e.last_change = ev.at
                else:
                    e.events.append(ChainEvent(at=now, kind="observed", text="CMU začalo řetězec sledovat"))
                self._entries[c.id] = e
                self._dirty = True
                continue
            e.last_seen_in_view = now
            changed_state = False
            if state != e.state:
                self._event(e, now, "state", f"Stav: {STATE_TEXT.get(e.state, e.state)} → "
                            f"{STATE_TEXT.get(state, state)}", e.state, state)
                e.state, changed_state = state, True
            if hlth != e.health:
                self._event(e, now, "health", f"{HEALTH_TEXT.get(e.health, e.health)} → "
                            f"{HEALTH_TEXT.get(hlth, hlth)}", e.health, hlth)
                e.health, changed_state = hlth, True
            if changed_state:
                e.state_since, e.state_since_exact = now, True
            if not c.stale and versions != e.versions:
                old, new = e.versions, versions
                for name in sorted(set(old) | set(new)):
                    if name not in old:
                        self._event(e, now, "apps", f"Přidána aplikace {name} ({new[name]})", None, new[name])
                    elif name not in new:
                        self._event(e, now, "apps", f"Odebrána aplikace {name}", old[name], None)
                    elif old[name] != new[name]:
                        self._event(e, now, "version", f"{name}: {old[name]} → {new[name]}", old[name], new[name])
                e.versions = versions
            if not c.stale and alerts != e.alerts:
                for a in sorted(alerts - e.alerts):
                    sev, name = a.split("|", 1)
                    self._event(e, now, "alert", f"Nová výstraha ({sev}): {name}", None, sev)
                for a in sorted(e.alerts - alerts):
                    sev, name = a.split("|", 1)
                    self._event(e, now, "alert", f"Výstraha skončila: {name}", sev, None)
                e.alerts = alerts
        # řetězce, které dlouho nejsou vidět, zapomenout
        cutoff = now - self._retention
        for cid in [k for k, v in self._entries.items() if v.last_seen_in_view < cutoff]:
            del self._entries[cid]
            self._dirty = True

    def flush(self) -> None:
        if self._dirty:
            self._store.save({k: v.to_json() for k, v in self._entries.items()})
            self._dirty = False

    # ------------------------------------------------------------------ čtení
    def decorate(self, chains: list[Chain]) -> None:
        for c in chains:
            e = self._entries.get(c.id)
            if e is None:
                continue
            c.stateSince, c.stateSinceExact = e.state_since, e.state_since_exact
            c.lastChangeAt, c.observedSince = e.last_change, e.observed_since

    def events(self, chain_id: str) -> list[ChainEvent] | None:
        e = self._entries.get(chain_id)
        return None if e is None else list(reversed(e.events))
