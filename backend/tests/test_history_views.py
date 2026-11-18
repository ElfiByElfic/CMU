"""Historie změn, uložené pohledy a filtry podle výstrah a změn."""

import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from fakes import FakeAgents
from fastapi.testclient import TestClient

from cmu_backend.api import create_app
from cmu_backend.history import ChangeTracker
from cmu_backend.model import Alert, Chain, ChainApp, ChainKind, ChainState, Health, LocationRef, Severity, SourceRef
from cmu_backend.settings import Settings
from cmu_backend.store import JsonStore
from cmu_backend.views import normalize_query

API = "/api/v1"


def chain(state: str = "running", health: str = "ok", version: str = "1.0", alerts: list[Alert] | None = None,
          stale: bool = False) -> Chain:
    return Chain(
        id="s:vm:c", localId="c", name="C", kind=ChainKind.process, source=SourceRef(id="s", type="agent"),
        location=LocationRef(id="s:vm", name="vm"), state=ChainState(state), health=Health(health), stale=stale,
        apps=[ChainApp(id="c.a", name="A", position=0, state="running", version=version)], alerts=alerts or [],
    )


class TrackerTest(unittest.TestCase):
    def test_tracks_changes_and_persists(self) -> None:
        with TemporaryDirectory() as d:
            t = ChangeTracker(JsonStore(Path(d), "h.json"))
            t0 = chain().model_copy()
            from cmu_backend.model import utcnow
            now = utcnow()
            t.observe([t0], now)
            c = chain()
            t.decorate([c])
            self.assertFalse(c.stateSinceExact, "změnu jsme neviděli → jen „nejméně od“")
            self.assertIsNone(c.lastChangeAt)
            self.assertEqual(c.observedSince, now)

            t.observe([chain("broken", "problem")], now + timedelta(minutes=1))
            t.observe([chain("broken", "problem", version="1.1")], now + timedelta(minutes=2))
            alert = Alert(source="zabbix", severity=Severity.high, name="Disk plný")
            t.observe([chain("broken", "problem", version="1.1", alerts=[alert])], now + timedelta(minutes=3))
            # nedostupný hostitel: verze a výstrahy se neporovnávají (data nejsou aktuální)
            t.observe([chain("unknown", "unknown", version="?", stale=True)], now + timedelta(minutes=4))
            c = chain()
            t.decorate([c])
            self.assertTrue(c.stateSinceExact)
            self.assertEqual(c.stateSince, now + timedelta(minutes=4))
            self.assertEqual(c.lastChangeAt, now + timedelta(minutes=4))
            texts = [e.text for e in t.events("s:vm:c") or []]
            self.assertEqual(texts, [
                "Problém → Nedostupné", "Stav: přerušený → stav neznámý",
                "Nová výstraha (high): Disk plný", "A: 1.0 → 1.1",
                "V pořádku → Problém", "Stav: běží → přerušený", "CMU začalo řetězec sledovat",
            ])
            t.flush()
            again = ChangeTracker(JsonStore(Path(d), "h.json"))
            self.assertEqual([e.text for e in again.events("s:vm:c") or []], texts)

    def test_normalize_query(self) -> None:
        self.assertEqual(normalize_query("?host=b&health=problem&host=a&q="), "health=problem&host=a&host=b")


class ApiTest(unittest.TestCase):
    def test_views_crud_and_persistence(self) -> None:
        with TemporaryDirectory() as d:
            s = Settings.model_validate({"dataDir": d, "auth": {"viewerTokens": ["v"], "operatorTokens": ["o"]},
                                         "sources": [{"type": "demo"}]})
            with TestClient(create_app(s)) as c:
                op = {"Authorization": "Bearer o"}
                view = {"Authorization": "Bearer v"}
                r = c.post(f"{API}/views", json={"name": "Produkce – problémy",
                                                 "query": "health=problem&env=production"}, headers=op)
                self.assertEqual(r.status_code, 201)
                v = r.json()
                self.assertEqual(v["query"], "env=production&health=problem")
                dup = c.post(f"{API}/views", json={"name": "produkce – PROBLÉMY"}, headers=op)
                self.assertEqual(dup.status_code, 409)
                self.assertEqual(c.post(f"{API}/views", json={"name": "X"}, headers=view).status_code, 403)
                self.assertEqual([x["name"] for x in c.get(f"{API}/views", headers=view).json()],
                                 ["Produkce – problémy"])
                r = c.put(f"{API}/views/{v['id']}", json={"name": "Problémy", "query": "health=problem"}, headers=op)
                self.assertEqual(r.json()["name"], "Problémy")
                self.assertEqual(c.put(f"{API}/views/nope", json={"name": "Y"}, headers=op).status_code, 404)
            # po restartu backendu pohled zůstal
            with TestClient(create_app(s)) as c:
                lst = c.get(f"{API}/views", headers={"Authorization": "Bearer v"}).json()
                self.assertEqual([(x["name"], x["query"]) for x in lst], [("Problémy", "health=problem")])
                self.assertEqual(c.delete(f"{API}/views/{lst[0]['id']}",
                                          headers={"Authorization": "Bearer o"}).status_code, 204)
                self.assertEqual(c.get(f"{API}/views", headers={"Authorization": "Bearer o"}).json(), [])
            self.assertTrue(Path(d, "history.json").exists())

    def test_demo_history_and_filters(self) -> None:
        s = Settings.model_validate({"sources": [{"type": "demo", "stepS": 0.05}]})
        with TestClient(create_app(s)) as c:
            ch = c.get(f"{API}/chains/demo:vm-prod-03:chain-a").json()
            self.assertTrue(ch["stateSinceExact"])
            self.assertIsNotNone(ch["lastChangeAt"])
            hist = c.get(f"{API}/chains/demo:vm-prod-03:chain-a/history").json()
            self.assertEqual(hist[0]["kind"], "health")
            self.assertIn("App2: 2.4.1 → 2.4.0", [e["text"] for e in hist])
            self.assertEqual(c.get(f"{API}/chains/nope/history").status_code, 404)

            def names(q: str) -> list[str]:
                return sorted(f"{x['name']}@{x['location']['name']}" for x in c.get(f"{API}/chains?{q}").json())

            self.assertEqual(names("severity=high"), ["Export pro partnery@docker-01"])
            self.assertEqual(names("severity=average,high"),
                             ["Export pro partnery@docker-01", "Reporting@vm-prod-02"])
            self.assertEqual(len(names("severity=none")), len(names("")) - 2)
            from cmu_backend.model import utcnow
            hour_ago = quote((utcnow() - timedelta(hours=1)).isoformat())
            recent = names(f"changedSince={hour_ago}")
            self.assertIn("Příjem objednávek@vm-prod-03", recent)
            self.assertNotIn("Notifikace@docker-01", recent)
            month_ago = quote((utcnow() - timedelta(days=30)).isoformat())
            self.assertEqual(names(f"unchangedSince={month_ago}"), ["Sandbox@docker-02"])


class AgentStateSinceTest(unittest.TestCase):
    def test_agent_chain_gets_observed_since(self) -> None:
        agents = FakeAgents()
        s = Settings.model_validate({"sources": [{"type": "agent", "id": "agents", "token": FakeAgents.READ,
                                                  "agents": [{"name": "vm-1", "address": "10.0.1.11"}]}]})
        with TestClient(create_app(s, {"agents": agents.transport()})) as c:
            ch = c.get(f"{API}/chains/agents:vm-1:chain-a").json()
            self.assertFalse(ch["stateSinceExact"])
            self.assertIsNotNone(ch["observedSince"])
            agents.chains["10.0.1.11"][0]["state"] = "degraded"
            c.portal.call(c.app.state.core.refresh_now)
            ch = c.get(f"{API}/chains/agents:vm-1:chain-a").json()
            self.assertEqual(ch["state"], "degraded")
            self.assertTrue(ch["stateSinceExact"])
            hist = c.get(f"{API}/chains/agents:vm-1:chain-a/history").json()
            self.assertEqual(hist[0]["text"], "V pořádku → Varování")
            self.assertEqual(hist[1]["text"], "Stav: běží → zpomalený")


if __name__ == "__main__":
    unittest.main()
