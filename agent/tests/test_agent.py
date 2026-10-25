"""End-to-end testy agenta se skutečnými procesy (tests/fake_app.py)."""

import json
import time
import unittest

from helpers import CONTROL, READ, AgentEnv, chain, wait_for, wait_op

API = "/agent/v1"


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.env = AgentEnv()

    def tearDown(self):
        self.env.cleanup()

    def start_chain(self, c, *types, name="Řetězec A"):
        r = c.post(f"{API}/chains?start=true", json=chain(name, *types))
        self.assertEqual(r.status_code, 202, r.text)
        op = wait_op(c, r.json())
        self.assertEqual(op["status"], "succeeded", op)
        return op["target"], op

    # ------------------------------------------------------------------ autorizace
    def test_auth_roles(self):
        with self.env.client(token=None) as c:
            self.assertEqual(c.get(f"{API}/health").status_code, 200)
            r = c.get(f"{API}/overview")
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.headers["content-type"], "application/problem+json")
            c.headers["Authorization"] = f"Bearer {READ}"
            self.assertEqual(c.get(f"{API}/overview").status_code, 200)
            self.assertEqual(c.post(f"{API}/chains", json=chain("X", "App1", "App2")).status_code, 403)
            c.headers["Authorization"] = "Bearer wrong"
            self.assertEqual(c.get(f"{API}/info").status_code, 401)

    # ------------------------------------------------------------------ validace
    def test_validation(self):
        with self.env.client() as c:
            r = c.post(f"{API}/validate", json=chain("A", "App1", "App2@2.0.0-rc1"))
            v = r.json()
            self.assertTrue(v["valid"], v)
            self.assertEqual(len(v["assignedPorts"]), 2)
            self.assertTrue(any("předběžná" in w for w in v["warnings"]))

            r = c.post(f"{API}/chains", json=chain("", "App1", "App1", "Nope@9"))
            self.assertEqual(r.status_code, 422)
            errors = " | ".join(r.json()["errors"])
            for part in ("chybí název", "dvakrát", "není v katalogu"):
                self.assertIn(part, errors)

            r = c.post(f"{API}/chains", json={"name": "x", "apps": "nope"})
            self.assertEqual(r.status_code, 422)
            self.assertEqual(r.headers["content-type"], "application/problem+json")

            self.assertEqual(c.post(f"{API}/chains", json=chain("Dup", "App1", "App2")).status_code, 201)
            r = c.post(f"{API}/chains", json=chain("dup", "App1", "App3"))
            self.assertIn("už na VM používá", r.json()["errors"][0])

    # ------------------------------------------------------------------ hlavní scénář
    def test_lifecycle(self):
        with self.env.client() as c:
            chain_id, op = self.start_chain(c, "App1", "App2", "App3")
            # spouštění odzadu
            self.assertEqual([s["target"] for s in op["steps"]],
                             [f"{chain_id}.app3", f"{chain_id}.app2", f"{chain_id}.app1"])

            ov = wait_for(lambda: (o := c.get(f"{API}/overview").json())["state"] == "ok" and o)
            ch = ov["chains"][0]
            self.assertEqual(ch["state"], "running")
            self.assertTrue(ch["desiredRunning"])
            apps = {a["type"]: a for a in ch["apps"]}
            self.assertTrue(all(a["state"] == "running" and a["ready"] for a in apps.values()))

            # --- konfigurace: aplikace (token instance) vs. administrátor (maskováno)
            manager = c.app.state.manager
            app1 = manager.find_app(f"{chain_id}.app1")[1]
            url = f"{API}/apps/{chain_id}.app1/config"
            r = c.get(url, headers={"Authorization": f"Bearer {app1.l.token}"})
            self.assertEqual(r.status_code, 200)
            self.assertIn("[database]", r.text)
            self.assertIn("password=s3cret", r.text)
            self.assertIn(f"downstreamUrl=http://127.0.0.1:{apps['App2']['port']}", r.text)
            self.assertIn(f"port={apps['App1']['port']}", r.text)
            etag = r.headers["etag"]
            r2 = c.get(url, headers={"Authorization": f"Bearer {app1.l.token}", "If-None-Match": etag})
            self.assertEqual(r2.status_code, 304)

            masked = c.get(url + "?format=json").json()
            self.assertEqual(masked["database"]["password"], "***")
            self.assertEqual(masked["chain"]["upstreamUrl"], "")

            # soubor s konfigurací, který si aplikace načetla při startu
            status = c.get(f"{API}/apps/{chain_id}.app1").json()["lastStatus"]
            self.assertEqual(status["extra"]["config"]["database"]["password"], "s3cret")
            other = c.get(f"{API}/apps/{chain_id}.app2/config", headers={"Authorization": f"Bearer {app1.l.token}"})
            self.assertEqual(other.status_code, 401, "token instance platí jen pro její konfiguraci")

            # --- logy
            lines = c.get(f"{API}/apps/{chain_id}.app2/logs?tail=10").json()["lines"]
            self.assertTrue(any("fake app" in ln for ln in lines), lines)

            # --- souběžná operace nad stejným řetězcem
            r = c.post(f"{API}/chains/{chain_id}/stop")
            self.assertEqual(r.status_code, 202)
            self.assertEqual(c.post(f"{API}/chains/{chain_id}/start").status_code, 409)
            op = wait_op(c, r.json())
            # zastavování odpředu
            self.assertEqual([s["target"] for s in op["steps"]],
                             [f"{chain_id}.app1", f"{chain_id}.app2", f"{chain_id}.app3"])
            ch = c.get(f"{API}/chains/{chain_id}").json()
            self.assertEqual(ch["state"], "stopped")
            self.assertFalse(ch["desiredRunning"])

        # --- požadovaný stav přežije restart agenta
        with self.env.client() as c:
            ch = c.get(f"{API}/chains/{chain_id}").json()
            self.assertEqual(ch["state"], "stopped")
            self.assertEqual(len(ch["apps"]), 3)
            op = wait_op(c, c.delete(f"{API}/chains/{chain_id}").json())
            self.assertEqual(op["status"], "succeeded")
            self.assertEqual(c.get(f"{API}/chains/{chain_id}").status_code, 404)

        audit = (self.env.cfg.logDir / "audit.log").read_text()
        self.assertIn(f"create {chain_id} +start", audit)
        self.assertNotIn(CONTROL, audit)

    def test_autostart_after_agent_restart(self):
        with self.env.client() as c:
            chain_id, _ = self.start_chain(c, "App2", "App3")
        # agent skončil → procesy zastaveny; po startu je agent znovu spustí
        with self.env.client() as c:
            ch = wait_for(lambda: (x := c.get(f"{API}/chains/{chain_id}").json())["state"] == "running" and x)
            self.assertTrue(all(a["restarts"] == 0 for a in ch["apps"]))

    # ------------------------------------------------------------------ změna konfigurace
    def test_config_change(self):
        with self.env.client() as c:
            chain_id, _ = self.start_chain(c, "App2", "App3")
            iid = f"{chain_id}.app2"

            r = c.patch(f"{API}/apps/{iid}/config", json={"logLevel": "DEBUG"})
            res = r.json()
            self.assertTrue(res["applied"], res)
            self.assertFalse(res["restartRequired"])

            res = c.patch(f"{API}/apps/{iid}/config", json={"timeoutMs": 8000}).json()
            self.assertFalse(res["applied"])
            self.assertTrue(res["restartRequired"])
            self.assertIn("restart", res["message"])
            self.assertEqual(c.get(f"{API}/overview").json()["state"], "warning")

            # reload: agent přepíše soubor, aplikace si ho znovu načte
            op = wait_op(c, c.post(f"{API}/apps/{iid}/reload").json())
            self.assertEqual(op["status"], "succeeded", op)
            st = wait_for(lambda: (s := c.get(f"{API}/apps/{iid}").json()) and s["lastStatus"]["extra"]["reloads"] == 1
                          and s)
            self.assertEqual(st["lastStatus"]["extra"]["config"]["timeoutMs"], 8000)
            self.assertFalse(st["configPending"])

            # merge patch: null maže klíč z vrstvy řetězce → zpět výchozí hodnota z katalogu
            c.patch(f"{API}/apps/{iid}/config", json={"timeoutMs": None})
            cfg = c.get(f"{API}/apps/{iid}/config").json()
            self.assertEqual(cfg["timeoutMs"], 5000)
            saved = json.loads((self.env.cfg.stateDir / "chains.json").read_text())
            self.assertEqual(saved[0]["spec"]["apps"][0]["config"], {"logLevel": "DEBUG"})

            r = c.patch(f"{API}/apps/{iid}/config", json={"chain": {"port": 1}})
            self.assertEqual(r.status_code, 422)
            r = c.patch(f"{API}/apps/{iid}/config", json={"pw": "secret://missing"})
            self.assertEqual(r.status_code, 422)

    # ------------------------------------------------------------------ chyby a pády
    def test_restart_after_crash(self):
        with self.env.client() as c:
            chain_id, _ = self.start_chain(c, "Crashy", "App3")
            iid = f"{chain_id}.crashy"
            app = wait_for(lambda: (a := c.get(f"{API}/apps/{iid}").json())["restarts"] >= 1 and a, timeout=20)
            self.assertGreaterEqual(app["restarts"], 1)
            # znovu spuštěná aplikace se v logu ohlásí podruhé
            wait_for(lambda: "\n".join(c.get(f"{API}/apps/{iid}/logs").json()["lines"]).count("fake app") >= 2)

    def test_start_failure(self):
        with self.env.client() as c:
            r = c.post(f"{API}/chains?start=true", json=chain("Rozbitý", "App2", "Broken"))
            op = wait_op(c, r.json())
            self.assertEqual(op["status"], "failed")
            failed = [s for s in op["steps"] if s["status"] == "failed"]
            self.assertEqual(failed[0]["target"].split(".")[1], "broken")
            self.assertIn("kód 3", failed[0]["message"])
            ch = c.get(f"{API}/chains/{op['target']}").json()
            self.assertEqual(ch["state"], "broken")
            self.assertEqual(c.get(f"{API}/overview").json()["state"], "problem")
            logs = c.get(f"{API}/apps/{op['target']}.broken/logs").json()["lines"]
            self.assertIn("cannot start", logs)

    def test_stop_timeout_uses_sigterm(self):
        with self.env.client() as c:
            chain_id, _ = self.start_chain(c, "Hangs", "App3")
            t0 = time.time()
            op = wait_op(c, c.post(f"{API}/apps/{chain_id}.hangs/stop").json())
            self.assertEqual(op["status"], "succeeded")
            self.assertIn("SIGTERM", op["message"])
            self.assertGreaterEqual(time.time() - t0, 1.4)
            self.assertEqual(c.get(f"{API}/apps/{chain_id}.hangs").json()["state"], "stopped")
            self.assertEqual(c.get(f"{API}/chains/{chain_id}").json()["state"], "broken")


if __name__ == "__main__":
    unittest.main()
