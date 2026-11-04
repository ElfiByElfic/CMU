"""End-to-end: backend ↔ skutečný chain-agent (HTTP) ↔ skutečné procesy (agent/tests/fake_app.py).

Spouští se, jen když je k dispozici balíček chain_agent (pip install ../agent).
"""

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

AGENT_TESTS = Path(__file__).resolve().parents[2] / "agent" / "tests"

try:
    import uvicorn
    from fastapi.testclient import TestClient

    import chain_agent  # noqa: F401
    sys.path.insert(0, str(AGENT_TESTS))
    from helpers import CONTROL, READ, AgentEnv  # type: ignore[import-not-found]

    from chain_agent.api import create_app as create_agent
    from cmu_backend.api import create_app
    from cmu_backend.settings import Settings
    HAVE_AGENT = True
except ImportError:  # pragma: no cover
    HAVE_AGENT = False

API = "/api/v1"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@unittest.skipUnless(HAVE_AGENT, "chain_agent není nainstalovaný")
class EndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env = AgentEnv()
        self.env.cfg.vmName = "vm-e2e"
        self.env.cfg.listenPort = free_port()
        config = uvicorn.Config(create_agent(self.env.cfg), host="127.0.0.1", port=self.env.cfg.listenPort,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.server.started, "agent se nespustil")

    def tearDown(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self.env.cleanup()

    def wait_op(self, c: TestClient, op: dict, timeout: float = 20) -> dict:
        deadline = time.time() + timeout
        while op["status"] == "running" and time.time() < deadline:
            time.sleep(0.2)
            op = c.get(f"{API}/operations/{op['id']}").json()
        return op

    def test_backend_drives_real_agent(self) -> None:
        s = Settings.model_validate({
            "pollIntervalS": 0.5,
            "sources": [{"type": "agent", "id": "agents", "token": READ, "controlToken": CONTROL,
                         "agents": [{"name": "vm-e2e", "address": "127.0.0.1", "port": self.env.cfg.listenPort}]}],
            "integrations": {"prometheus": {}},
        })
        with TestClient(create_app(s)) as c:
            loc = c.get(f"{API}/locations").json()[0]
            self.assertTrue(loc["reachable"], loc)
            self.assertEqual(loc["environment"], "production")

            cat = {x["type"] for x in c.get(f"{API}/targets/agents/vm-e2e/catalog").json()}
            self.assertTrue({"App1", "App2", "App3"} <= cat)

            body = {"sourceId": "agents", "location": "vm-e2e", "kind": "process", "name": "E2E řetězec",
                    "processApps": [{"type": "App2", "version": "1.0.0"}, {"type": "App3", "version": "1.0.0"}]}
            v = c.post(f"{API}/chains/validate", json=body).json()
            self.assertTrue(v["valid"], v)
            op = self.wait_op(c, c.post(f"{API}/chains", json=body).json())
            self.assertEqual(op["status"], "succeeded", op)
            self.assertEqual([x["target"].split(".")[1] for x in op["steps"]], ["app3", "app2"], "start odzadu")

            deadline = time.time() + 10
            while time.time() < deadline:
                chains = c.get(f"{API}/chains").json()
                if chains and chains[0]["health"] == "ok":
                    break
                time.sleep(0.2)
            ch = chains[0]
            self.assertEqual((ch["name"], ch["state"], ch["health"]), ("E2E řetězec", "running", "ok"))
            self.assertEqual(ch["location"]["name"], "vm-e2e")
            self.assertTrue(all(a["ready"] for a in ch["apps"]))

            logs = c.get(f"{API}/chains/{ch['id']}/apps/{ch['apps'][0]['id']}/logs").json()["lines"]
            self.assertTrue(any("fake app" in ln for ln in logs))

            sd = c.get(f"{API}/integrations/prometheus/targets").json()
            self.assertEqual(sd[0]["targets"], [f"127.0.0.1:{self.env.cfg.listenPort}"])

            op = self.wait_op(c, c.post(f"{API}/chains/{ch['id']}/actions/stop").json())
            self.assertEqual(op["status"], "succeeded", op)
            deadline = time.time() + 5
            while time.time() < deadline and c.get(f"{API}/chains/{ch['id']}").json()["state"] != "stopped":
                time.sleep(0.2)
            ch = c.get(f"{API}/chains/{ch['id']}").json()
            self.assertEqual((ch["state"], ch["health"]), ("stopped", "stopped"))

            op = self.wait_op(c, c.delete(f"{API}/chains/{ch['id']}").json())
            self.assertEqual(op["status"], "succeeded")

            # výpadek agenta → po staleAfter nedostupná lokalita, ne poslední stav
            self.server.should_exit = True
            self.thread.join(timeout=15)
            deadline = time.time() + 5
            while time.time() < deadline and c.get(f"{API}/locations").json()[0]["reachable"]:
                time.sleep(0.2)
            self.assertFalse(c.get(f"{API}/locations").json()[0]["reachable"])


if __name__ == "__main__":
    unittest.main()
