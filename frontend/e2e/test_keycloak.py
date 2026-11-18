"""E2E: přihlášení přes Keycloak v prohlížeči (Authorization Code + PKCE) proti simulovanému realmu.

Spustí simulovaný Keycloak (backend/tests/fake_keycloak.py) a backend s oidc; ověřuje návrat na
původní stránku, role ze skupin, obrazovku pro uživatele bez role, obnovu tokenu a odhlášení.
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "tests"))
from fake_keycloak import FakeKeycloak  # noqa: E402

STATIC = Path(os.environ.get("CMU_STATIC", ROOT / "frontend" / "dist"))
SHOTS = os.environ.get("CMU_SCREENSHOTS")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class KeycloakUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (STATIC / "index.html").is_file():
            raise unittest.SkipTest(f"chybí sestavený frontend {STATIC}")
        kc_port, port = free_port(), free_port()
        cls.kc = FakeKeycloak(base=f"http://127.0.0.1:{kc_port}", token_ttl=34)
        cls.kc_server = cls.kc.serve(kc_port)
        cfg = Path(tempfile.mkdtemp()) / "cmu.yaml"
        cfg.write_text(
            f"listenAddress: 127.0.0.1\nlistenPort: {port}\npollIntervalS: 2\nstaticDir: {STATIC}\n"
            f"auth:\n  operatorTokens: [machine]\n  oidc: {{issuer: '{cls.kc.issuer}', clientId: cmu}}\n"
            "sources: [{type: demo, stepS: 0.3}]\n")
        cls.backend = subprocess.Popen([sys.executable, "-m", "cmu_backend", "--config", str(cfg),
                                        "--log-level", "warning"])
        cls.url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.url + "/api/v1/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        cls.backend.terminate()
        cls.backend.wait(10)
        cls.kc_server.shutdown()

    def setUp(self) -> None:
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        self.ctx.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"),
                       lambda r: r.fulfill(status=200, body="", content_type="text/css"))
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def tearDown(self) -> None:
        self.ctx.close()
        self.assertEqual(self.errors, [])

    def sign_in(self, user: str, start: str = "/#/") -> None:
        p = self.page
        p.goto(self.url + start)
        p.get_by_role("button", name="Přihlásit se přes Keycloak").click()
        expect(p.get_by_role("heading", name="Keycloak (test)")).to_be_visible()
        p.get_by_role("link", name=f"Přihlásit jako {user}").click()

    def test_operator_login_returns_to_page_and_refreshes(self) -> None:
        p = self.page
        self.sign_in("operator", "/#/sources")
        expect(p.get_by_role("heading", name="Zdroje a integrace")).to_be_visible()
        self.assertNotIn("code=", p.url, "kód z Keycloaku nesmí zůstat v adrese")
        expect(p.locator(".topbar .role")).to_contain_text("operator")
        expect(p.locator(".topbar .role")).to_contain_text("Operátor")
        p.get_by_role("link", name="Řetězce").click()
        expect(p.get_by_role("link", name="Nový řetězec")).to_be_visible()
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/11-keycloak-operator.png")

        # token platí 34 s, obnovuje se 30 s před koncem → za ~5 s musí proběhnout refresh
        before = self.kc.refresh_grants
        time.sleep(5)
        p.get_by_role("link", name="Zdroje a integrace").click()
        expect(p.get_by_role("heading", name="Zdroje a integrace")).to_be_visible()
        expect(p.locator(".comp").first).to_be_visible()
        self.assertGreater(self.kc.refresh_grants, before, "přístupový token se měl obnovit refresh tokenem")
        expect(p.locator(".topbar .role")).to_contain_text("Operátor")

        # po obnovení stránky zůstává přihlášený (sessionStorage), odhlášení přes Keycloak
        p.reload()
        expect(p.locator(".topbar .role")).to_contain_text("operator")
        p.get_by_role("button", name="Odhlásit").click()
        expect(p.get_by_role("button", name="Přihlásit se přes Keycloak")).to_be_visible()

    def test_viewer_cannot_operate(self) -> None:
        p = self.page
        self.sign_in("viewer")
        expect(p.locator(".topbar .role")).to_contain_text("Jen čtení")
        expect(p.locator(".tile").first).to_be_visible()
        expect(p.get_by_role("link", name="Nový řetězec")).to_have_count(0)
        p.locator(".tile", has_text="vm-prod-01").first.click()
        expect(p.get_by_role("button", name="Restartovat", exact=True)).to_have_count(0)

    def test_user_without_group_gets_explanation(self) -> None:
        p = self.page
        self.sign_in("nobody")
        expect(p.get_by_role("heading", name="Nemáte přístup")).to_be_visible()
        expect(p.locator("main")).to_contain_text("/hr")
        expect(p.get_by_role("heading", name="Přihlášení")).to_have_count(0)
        if SHOTS:
            p.screenshot(path=f"{SHOTS}/12-keycloak-no-role.png")

    def test_machine_token_still_possible(self) -> None:
        p = self.page
        p.goto(self.url + "/#/")
        p.get_by_role("button", name="Přihlásit API tokenem").click()
        p.get_by_label("Token").fill("machine")
        p.get_by_role("button", name="Přihlásit tokenem").click()
        expect(p.locator(".topbar .role")).to_contain_text("API token")
        expect(p.locator(".tile").first).to_be_visible()


if __name__ == "__main__":
    unittest.main()
