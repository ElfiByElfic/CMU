"""Přihlášení přes Keycloak (OIDC): ověření tokenů a role ze skupin LDAPu."""

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fake_keycloak import FakeKeycloak
from fastapi.testclient import TestClient

from cmu_backend.api import create_app
from cmu_backend.auth import group_matches
from cmu_backend.settings import Settings

API = "/api/v1"


class KeycloakTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kc = FakeKeycloak()
        self.tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def client(self, **oidc) -> TestClient:
        s = Settings.model_validate({
            "dataDir": self.tmp.name,
            "auth": {"operatorTokens": ["machine-op"], "oidc": {
                "issuer": self.kc.issuer, "clientId": "cmu",
                "operatorRoles": ["cmu-operators"], "viewerRoles": ["cmu-viewers"], **oidc}},
            "sources": [{"type": "demo", "stepS": 0.05}],
        })
        return TestClient(create_app(s, {"oidc": self.kc.transport()}))

    def get(self, c: TestClient, path: str, token: str):
        return c.get(f"{API}{path}", headers={"Authorization": f"Bearer {token}"})

    def test_roles_from_ldap_groups(self) -> None:
        with self.client() as c:
            me = self.get(c, "/me", self.kc.token("operator")).json()
            self.assertEqual((me["role"], me["user"], me["via"]), ("operator", "operator", "oidc"))
            self.assertEqual(me["groups"], ["/cmu-operators", "offline_access"])
            # skupina ve stromu LDAPu (/it/cmu-viewers) odpovídá nastavenému „cmu-viewers“
            me = self.get(c, "/me", self.kc.token("viewer")).json()
            self.assertEqual((me["role"], me["user"]), ("viewer", "viewer"))
            self.assertEqual(self.get(c, "/chains", self.kc.token("viewer")).status_code, 200)
            r = c.post(f"{API}/chains/demo:vm-prod-01:chain-a/actions/stop",
                       headers={"Authorization": f"Bearer {self.kc.token('viewer')}"})
            self.assertEqual(r.status_code, 403)
            # přihlášený, ale bez skupiny CMU → 403 s vysvětlením (ne 401 – frontend by se zacyklil)
            r = self.get(c, "/chains", self.kc.token("nobody"))
            self.assertEqual(r.status_code, 403)
            self.assertIn("nemá v CMU žádnou roli", r.json()["detail"])

    def test_rejects_bad_tokens(self) -> None:
        with self.client() as c:
            cases = {
                "vypršel": self.kc.token(expires_in=-120),
                "jiný realm": self.kc.token(issuer="https://kc.test/realms/jiny"),
                "jiný klient": self.kc.token(azp="jina-aplikace"),
                "refresh token": self.kc.token(typ="Refresh"),
                "cizí klíč": self.kc.token(key=FakeKeycloak().key),
                "nesmysl": "a.b.c",
            }
            for name, tok in cases.items():
                with self.subTest(name):
                    r = self.get(c, "/chains", tok)
                    self.assertEqual(r.status_code, 401, r.text)
            r = self.get(c, "/chains", self.kc.token(expires_in=-120))
            self.assertIn("vypršela", r.json()["detail"])

    def test_key_rotation_and_machine_tokens(self) -> None:
        with self.client() as c:
            self.assertEqual(self.get(c, "/chains", self.kc.token()).status_code, 200)
            calls = self.kc.jwks_calls
            self.kc.rotate()
            self.assertEqual(self.get(c, "/chains", self.kc.token()).status_code, 200, "nový klíč se načte")
            self.assertEqual(self.kc.jwks_calls, calls + 1)
            # statický token pro stroje (Prometheus SD, skripty) funguje dál
            me = self.get(c, "/me", "machine-op").json()
            self.assertEqual((me["role"], me["via"]), ("operator", "token"))
            self.assertEqual(self.get(c, "/chains", "spatny").status_code, 401)

    def test_audience_mode(self) -> None:
        with self.client(audience="account") as c:
            self.assertEqual(self.get(c, "/chains", self.kc.token(azp="jiny-klient")).status_code, 200)
        with self.client(audience="cmu-backend") as c:
            self.assertEqual(self.get(c, "/chains", self.kc.token()).status_code, 401)

    def test_public_config_audit_and_view_author(self) -> None:
        with self.client() as c:
            cfg = c.get(f"{API}/auth/config").json()
            self.assertEqual(cfg["mode"], "oidc")
            self.assertEqual(cfg["clientId"], "cmu")
            self.assertTrue(cfg["authorizationEndpoint"].endswith("/protocol/openid-connect/auth"))
            self.assertTrue(cfg["tokensAllowed"])
            tok = self.kc.token("operator")
            r = c.post(f"{API}/views", json={"name": "Moje", "query": "health=problem"},
                       headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(r.json()["createdBy"], "operator")
            r = c.post(f"{API}/chains/demo:vm-prod-01:chain-a/actions/restart",
                       headers={"Authorization": f"Bearer {tok}"})
            self.assertEqual(r.status_code, 202)
            time.sleep(0.1)
        lines = [json.loads(x) for x in Path(self.tmp.name, "audit.log").read_text().splitlines()]
        self.assertEqual([(x["user"], x["action"]) for x in lines],
                         [("operator", "view.create"), ("operator", "chain.restart")])
        self.assertEqual(lines[1]["target"], "demo:vm-prod-01:chain-a")

    def test_keycloak_unreachable(self) -> None:
        s = Settings.model_validate({"auth": {"oidc": {"issuer": "https://down.test/realms/cmu"}},
                                     "sources": [{"type": "demo"}]})
        import httpx

        def down(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=req)
        with TestClient(create_app(s, {"oidc": httpx.MockTransport(down)})) as c:
            r = c.get(f"{API}/chains", headers={"Authorization": f"Bearer {self.kc.token()}"})
            self.assertEqual(r.status_code, 401)
            self.assertIn("Keycloak je nedostupný", r.json()["detail"])
            self.assertIn("error", c.get(f"{API}/auth/config").json())

    def test_group_matching(self) -> None:
        self.assertTrue(group_matches("/cmu-operators", "cmu-operators"))
        self.assertTrue(group_matches("/it/cmu-operators", "cmu-operators"))
        self.assertTrue(group_matches("/it/CMU-Operators", "/it/cmu-operators"))
        self.assertFalse(group_matches("/hr/cmu-operators", "/it/cmu-operators"))
        self.assertFalse(group_matches("/cmu-operators-old", "cmu-operators"))


if __name__ == "__main__":
    unittest.main()
