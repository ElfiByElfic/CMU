"""Testy backendu proti simulovaným API agenta, Komoda, Zabbixu a Prometheu."""

import time
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from fakes import FakeAgents, FakeKomodo, FakePrometheus, FakeZabbix
from fastapi.testclient import TestClient

from cmu_backend.api import create_app
from cmu_backend.settings import Settings

API = "/api/v1"


def settings(**overrides) -> Settings:
    base = {
        "pollIntervalS": 60,
        "auth": {"viewerTokens": ["view"], "operatorTokens": ["op"]},
        "sources": [
            {"type": "agent", "id": "agents", "token": FakeAgents.READ, "controlToken": FakeAgents.CONTROL,
             "agents": [{"name": "vm-1", "address": "10.0.1.11", "environment": "production"},
                        {"name": "vm-2", "address": "10.0.1.12"},
                        {"name": "vm-3", "address": "10.0.1.13"}]},
            {"type": "komodo", "id": "komodo", "url": "https://komodo.test", "apiKey": FakeKomodo.KEY,
             "apiSecret": FakeKomodo.SECRET, "environment": "production"},
        ],
        "integrations": {
            "zabbix": {"url": "https://zbx.test/api_jsonrpc.php", "token": FakeZabbix.TOKEN,
                       "hostMap": {"docker-01": "zbx-docker-01"}, "webUrl": "https://zbx.test"},
            "prometheus": {"url": "https://prom.test", "metrics": [
                {"id": "throughput", "label": "Zpráv/min", "unit": "/min",
                 "query": "sum by (vm, chain) (rate(chain_messages_processed_total[5m])) * 60"}]},
        },
    }
    base.update(overrides)
    return Settings.model_validate(base)


class BackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agents, self.komodo = FakeAgents(), FakeKomodo()
        self.zabbix, self.prom = FakeZabbix(), FakePrometheus()
        self.agents.down.add("10.0.1.13")

    def client(self, s: Settings | None = None, token: str | None = "op") -> TestClient:
        transports = {"agents": self.agents.transport(), "komodo": self.komodo.transport(),
                      "zabbix": self.zabbix.transport(), "prometheus": self.prom.transport()}
        c = TestClient(create_app(s or settings(), transports))
        if token:
            c.headers["Authorization"] = f"Bearer {token}"
        return c

    # ------------------------------------------------------------------ pohled
    def test_tiles_are_chains_with_location(self) -> None:
        with self.client() as c:
            chains = c.get(f"{API}/chains").json()
            by_id = {x["id"]: x for x in chains}
            self.assertEqual(sorted(by_id), ["agents:vm-1:chain-a", "agents:vm-2:chain-b",
                                             "komodo:docker-01:cmu-notifikace"])
            a = by_id["agents:vm-1:chain-a"]
            self.assertEqual(a["location"], {"id": "agents:vm-1", "name": "vm-1", "address": "10.0.1.11",
                                             "environment": "production", "kind": "vm"})
            self.assertEqual(a["kind"], "process")
            # řazení: nejdřív problémy
            self.assertEqual(chains[0]["health"], "problem")

            k = by_id["komodo:docker-01:cmu-notifikace"]
            self.assertEqual(k["name"], "Notifikace")
            self.assertEqual(k["kind"], "container")
            self.assertEqual([x["name"] for x in k["apps"]], ["gateway", "router", "sender"], "pořadí z labelů")
            self.assertEqual(k["apps"][1]["state"], "stopped")
            self.assertEqual(k["location"]["address"], "10.0.3.31")

            locs = {x["name"]: x for x in c.get(f"{API}/locations").json()}
            self.assertFalse(locs["vm-3"]["reachable"])
            self.assertIn("nedostupný", locs["vm-3"]["error"])
            self.assertFalse(locs["docker-02"]["reachable"])
            self.assertTrue(locs["vm-1"]["reachable"])

            comps = {x["id"]: x for x in c.get(f"{API}/components").json()}
            self.assertFalse(comps["agents"]["ok"])
            self.assertEqual(comps["agents"]["details"], {"agents": 3, "unreachable": 1})
            self.assertTrue(comps["komodo"]["ok"])
            self.assertTrue(comps["zabbix"]["ok"])

    def test_filters_and_summary(self) -> None:
        with self.client() as c:
            self.assertEqual(len(c.get(f"{API}/chains?kind=container").json()), 1)
            self.assertEqual(len(c.get(f"{API}/chains?location=vm-2").json()), 1)
            self.assertEqual(len(c.get(f"{API}/chains?q=gateway").json()), 1)
            self.assertEqual(len(c.get(f"{API}/chains?environment=production").json()), 3)
            s = c.get(f"{API}/summary").json()
            self.assertEqual(s["chains"], 3)
            self.assertEqual(sum(s["byHealth"].values()), 3)
            self.assertEqual(s["unreachableLocations"], 2)

    def test_stale_location_never_shows_last_state(self) -> None:
        with self.client() as c:
            core = c.app.state.core
            src = core.source("agents")
            # data vm-1 jsou starší než staleAfter
            src._agents["vm-1"].last_ok -= timedelta(seconds=src.stale_after_s + 1)
            ch = c.get(f"{API}/chains/agents:vm-1:chain-a").json()
            self.assertTrue(ch["stale"])
            self.assertEqual(ch["state"], "unknown")
            self.assertEqual(ch["health"], "unknown")
            self.assertEqual({a["state"] for a in ch["apps"]}, {"unknown"})
            r = c.post(f"{API}/chains/agents:vm-1:chain-a/actions/stop")
            self.assertEqual(r.status_code, 409)

    # ------------------------------------------------------------------ autorizace
    def test_roles(self) -> None:
        with self.client(token=None) as c:
            self.assertEqual(c.get(f"{API}/health").status_code, 200)
            r = c.get(f"{API}/chains")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.headers["content-type"], "application/problem+json")
            c.headers["Authorization"] = "Bearer view"
            self.assertEqual(c.get(f"{API}/chains").status_code, 200)
            self.assertEqual(c.get(f"{API}/me").json(), {"role": "viewer", "authEnabled": True})
            self.assertEqual(c.post(f"{API}/chains/agents:vm-1:chain-a/actions/stop").status_code, 403)

    # ------------------------------------------------------------------ akce: agent
    def test_agent_actions_use_control_token(self) -> None:
        with self.client() as c:
            op = c.post(f"{API}/chains/agents:vm-1:chain-a/actions/stop").json()
            self.assertEqual(op["id"], "agents:vm-1:op1")
            self.assertIn(("10.0.1.11", "POST", "/chains/chain-a/stop", FakeAgents.CONTROL), self.agents.calls)
            done = c.get(f"{API}/operations/{op['id']}").json()
            self.assertEqual(done["status"], "succeeded")
            logs = c.get(f"{API}/chains/agents:vm-1:chain-a/apps/chain-a.app1/logs?tail=5").json()
            self.assertEqual(logs["lines"], ["line 1", "line 2"])
            self.assertEqual(c.post(f"{API}/chains/agents:vm-1:chain-a/apps/nope/actions/start").status_code, 404)
            self.assertEqual(c.post(f"{API}/chains/agents:vm-1:chain-a/actions/explode").status_code, 409)
            self.assertEqual(c.get(f"{API}/chains/agents:vm-9:x").status_code, 404)

    def test_agent_create_and_catalog(self) -> None:
        with self.client() as c:
            targets = c.get(f"{API}/targets?kind=process").json()
            self.assertEqual([t["location"] for t in targets], ["vm-1", "vm-2", "vm-3"])
            self.assertFalse(targets[2]["reachable"])
            self.assertEqual(c.get(f"{API}/targets/agents/vm-1/catalog").json(),
                             [{"type": "App1", "versions": ["1.0.0"]}])
            body = {"sourceId": "agents", "location": "vm-1", "kind": "process", "name": "Nový",
                    "processApps": [{"type": "App1", "version": "1.0.0"}]}
            v = c.post(f"{API}/chains/validate", json=body).json()
            self.assertFalse(v["valid"])
            body["processApps"].append({"type": "App2", "version": "1.0.0"})
            self.assertTrue(c.post(f"{API}/chains/validate", json=body).json()["valid"])
            op = c.post(f"{API}/chains", json={**body, "start": False}).json()
            self.assertEqual(op["status"], "succeeded")
            op = c.post(f"{API}/chains", json=body).json()
            self.assertEqual(op["id"], "agents:vm-1:op2")
            r = c.get(f"{API}/targets/agents/vm-3/catalog")
            self.assertEqual(r.status_code, 502)
            self.assertIn("nedostupný", r.json()["detail"])

    # ------------------------------------------------------------------ akce: Komodo
    def test_komodo_actions_and_create(self) -> None:
        with self.client() as c:
            cid = "komodo:docker-01:cmu-notifikace"
            op = c.post(f"{API}/chains/{cid}/actions/restart").json()
            self.assertEqual(op["status"], "running")
            self.assertEqual(self.komodo.calls[-1], ("execute", "RestartStack", {"stack": "cmu-notifikace"}))
            done = c.get(f"{API}/operations/{op['id']}").json()
            self.assertEqual(done["status"], "succeeded")
            self.assertEqual(done["steps"][0]["target"], "Compose")

            c.post(f"{API}/chains/{cid}/apps/router/actions/start")
            self.assertEqual(self.komodo.calls[-1],
                             ("execute", "StartStack", {"stack": "cmu-notifikace", "services": ["router"]}))
            self.assertEqual(c.get(f"{API}/chains/{cid}/apps/router/logs").json()["lines"], ["a", "b"])

            body = {"sourceId": "komodo", "location": "docker-01", "kind": "container", "name": "Export dat",
                    "containerApps": [{"name": "collector", "image": "reg/collector:1.0", "port": 9000},
                                      {"name": "packer", "image": "reg/packer:1.0", "publish": 18080}]}
            v = c.post(f"{API}/chains/validate", json=body).json()
            self.assertTrue(v["valid"], v)
            compose = yaml.safe_load(v["preview"])
            self.assertEqual(compose["name"], "cmu-export-dat")
            col, pck = compose["services"]["collector"], compose["services"]["packer"]
            self.assertEqual(col["environment"]["CHAIN_DOWNSTREAM_URL"], "http://packer:8080")
            self.assertEqual(pck["environment"]["CHAIN_UPSTREAM_URL"], "http://collector:9000")
            self.assertEqual(col["depends_on"], {"packer": {"condition": "service_healthy"}}, "start odzadu")
            self.assertEqual(pck["ports"], ["18080:8080"])
            self.assertEqual(pck["labels"]["cmu.chain.position"], "1")

            op = c.post(f"{API}/chains", json=body).json()
            create = [x for x in self.komodo.calls if x[1] == "CreateStack"][0][2]
            self.assertEqual(create["name"], "cmu-export-dat")
            self.assertEqual(create["config"]["server_id"], "s1")
            self.assertEqual(yaml.safe_load(create["config"]["file_contents"]), compose)
            self.assertEqual(self.komodo.calls[[x[1] for x in self.komodo.calls].index("CreateStack") + 1][1],
                             "DeployStack")
            self.assertEqual(op["kind"], "DeployStack")
            again = c.post(f"{API}/chains/validate", json=body).json()
            self.assertIn("už v Komodu existuje", " ".join(again["errors"]))

            body2 = {**body, "location": "docker-99", "name": "X",
                     "containerApps": [{"name": "a", "image": "i:1"}, {"name": "a", "image": "i:1"}]}
            r = c.post(f"{API}/chains", json=body2)
            self.assertEqual(r.status_code, 422)
            self.assertTrue(any("vícekrát" in e for e in r.json()["errors"]))

            op = c.delete(f"{API}/chains/{cid}").json()
            self.assertTrue(op["id"].startswith("local:"))
            for _ in range(50):
                op = c.get(f"{API}/operations/{op['id']}").json()
                if op["status"] != "running":
                    break
                time.sleep(0.1)
            self.assertEqual(op["status"], "succeeded", op)
            self.assertEqual([s["target"] for s in op["steps"]], ["DestroyStack", "DeleteStack"])
            self.assertEqual(c.get(f"{API}/chains/{cid}").status_code, 404)

    def test_komodo_bad_credentials(self) -> None:
        s = settings()
        s.sources[1].apiSecret = "wrong"  # type: ignore[union-attr]
        with self.client(s) as c:
            comps = {x["id"]: x for x in c.get(f"{API}/components").json()}
            self.assertFalse(comps["komodo"]["ok"])
            self.assertIn("invalid api key", comps["komodo"]["message"])
            self.assertEqual(len(c.get(f"{API}/chains?kind=container").json()), 0)

    # ------------------------------------------------------------------ integrace
    def test_zabbix_alerts_escalate_health(self) -> None:
        with self.client() as c:
            a = c.get(f"{API}/chains/agents:vm-1:chain-a").json()
            self.assertEqual(len(a["alerts"]), 1)
            self.assertEqual(a["alerts"][0]["severity"], "high")
            self.assertTrue(a["alerts"][0]["url"].endswith("triggerids%5B%5D=9001"))
            self.assertEqual(a["health"], "problem", "běžící řetězec s výstrahou high je problém")
            k = c.get(f"{API}/chains/komodo:docker-01:cmu-notifikace").json()
            self.assertEqual([x["name"] for x in k["alerts"]], ["Vysoká zátěž CPU"], "hostMap docker-01")
            method, params = self.zabbix.calls[-1]
            self.assertEqual(method, "trigger.get")
            self.assertEqual(params["filter"], {"value": 1})
            self.assertEqual(sorted(params["hostids"]), ["101", "102"])
            locs = {x["name"]: x for x in c.get(f"{API}/locations").json()}
            self.assertEqual(len(locs["vm-1"]["alerts"]), 1)

    def test_zabbix_failure_does_not_break_view(self) -> None:
        self.zabbix.fail = True
        with self.client() as c:
            self.assertEqual(len(c.get(f"{API}/chains").json()), 3)
            comps = {x["id"]: x for x in c.get(f"{API}/components").json()}
            self.assertFalse(comps["zabbix"]["ok"])

    def test_prometheus_metrics_and_service_discovery(self) -> None:
        with self.client() as c:
            a = c.get(f"{API}/chains/agents:vm-1:chain-a").json()
            self.assertEqual(a["metrics"], [{"id": "throughput", "label": "Zpráv/min", "value": 1234.5,
                                             "unit": "/min", "source": "prometheus"}])
            b = c.get(f"{API}/chains/agents:vm-2:chain-b").json()
            self.assertIsNone(b["metrics"][0]["value"], "NaN → null")
            sd = c.get(f"{API}/integrations/prometheus/targets").json()
            self.assertEqual(sd[0], {"targets": ["10.0.1.11:9100"], "labels": {
                "__metrics_path__": "/agent/v1/metrics", "vm": "vm-1", "cmu_source": "agents",
                "environment": "production"}})
            self.assertEqual(len(sd), 3)

    def test_inventory_file_reload(self) -> None:
        with TemporaryDirectory() as d:
            inv = Path(d, "agents.yaml")
            inv.write_text(yaml.safe_dump([{"name": "vm-1", "address": "10.0.1.11"}]))
            s = settings(integrations={})
            s.sources = [s.sources[0]]
            s.sources[0].agents = []  # type: ignore[union-attr]
            s.sources[0].inventoryFile = inv  # type: ignore[union-attr]
            with self.client(s) as c:
                self.assertEqual([x["name"] for x in c.get(f"{API}/locations").json()], ["vm-1"])
                inv.write_text(yaml.safe_dump([{"name": "vm-1", "address": "10.0.1.11"},
                                               {"name": "vm-2", "address": "10.0.1.12"}]))
                import os
                os.utime(inv, (time.time() + 5, time.time() + 5))
                c.portal.call(c.app.state.core.refresh_now)
                self.assertEqual(sorted(x["name"] for x in c.get(f"{API}/locations").json()), ["vm-1", "vm-2"])
                self.assertEqual(len(c.get(f"{API}/chains").json()), 2)

    def test_settings_env_expansion(self) -> None:
        import os
        os.environ["CMU_TEST_TOKEN"] = "secret-x"
        with TemporaryDirectory() as d:
            p = Path(d, "cmu.yaml")
            p.write_text("auth:\n  viewerTokens: ['${CMU_TEST_TOKEN}']\nsources:\n"
                         "  - {type: demo}\nintegrations:\n  prometheus: {token: '${MISSING_VAR:-}'}\n")
            s = Settings.load(p)
            self.assertEqual(s.auth.viewerTokens, ["secret-x"])
            self.assertEqual(s.integrations.prometheus.token, "")  # type: ignore[union-attr]


class DemoTest(unittest.TestCase):
    def test_demo_flow(self) -> None:
        s = Settings.model_validate({"sources": [{"type": "demo", "stepS": 0.05}]})
        with TestClient(create_app(s)) as c:
            chains = c.get(f"{API}/chains").json()
            self.assertGreaterEqual(len(chains), 10)
            self.assertEqual({x["kind"] for x in chains}, {"process", "container"})
            unknown = [x for x in chains if x["health"] == "unknown"]
            self.assertTrue(unknown and all(x["stale"] for x in unknown))
            body = {"sourceId": "demo", "location": "docker-02", "kind": "container", "name": "Test",
                    "containerApps": [{"name": "a", "image": "i/a:1"}, {"name": "b", "image": "i/b:1"}]}
            op = c.post(f"{API}/chains", json=body).json()
            for _ in range(60):
                op = c.get(f"{API}/operations/{op['id']}").json()
                if op["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(op["status"], "succeeded")
            self.assertEqual([s["target"] for s in op["steps"]], ["b", "a"], "start odzadu")
            new = [x for x in c.get(f"{API}/chains?location=docker-02").json() if x["name"] == "Test"]
            self.assertEqual(new[0]["state"], "running")
            self.assertEqual(new[0]["id"], "demo:docker-02:cmu-test")
            r = c.post(f"{API}/chains", json={**body, "location": "vm-prod-01"})
            self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
