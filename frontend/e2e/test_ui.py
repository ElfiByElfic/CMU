"""E2E test GUI v prohlížeči proti backendu v demo režimu.

Test si sám spustí backend (čerstvý stav demo dat) se sestaveným frontendem:
    npm run build                       # → frontend/dist
    pytest frontend/e2e -v              # potřebuje nainstalovaný cmu-backend a playwright
Proti už běžícímu backendu: CMU_URL=http://… (stav dem dat se mezi běhy nemaže).
Jiný adresář s frontendem: CMU_STATIC=…; snímky obrazovky: CMU_SCREENSHOTS=…
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

from playwright.sync_api import Page, expect, sync_playwright

URL = os.environ.get("CMU_URL", "")
STATIC = Path(os.environ.get("CMU_STATIC", Path(__file__).resolve().parents[1] / "dist"))
SHOTS = os.environ.get("CMU_SCREENSHOTS")
OPERATOR = os.environ.get("CMU_OPERATOR_TOKEN", "op-token")


class UiTest(unittest.TestCase):
    backend: subprocess.Popen[bytes] | None = None

    @classmethod
    def setUpClass(cls) -> None:
        global URL
        if not URL:
            URL = cls._start_backend()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.pw.stop()
        if cls.backend:
            cls.backend.terminate()
            cls.backend.wait(10)

    @classmethod
    def _start_backend(cls) -> str:
        if not (STATIC / "index.html").is_file():
            raise unittest.SkipTest(f"chybí sestavený frontend {STATIC} (npm run build)")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cfg = Path(tempfile.mkdtemp()) / "cmu.yaml"
        cfg.write_text(
            f"listenAddress: 127.0.0.1\nlistenPort: {port}\npollIntervalS: 2\nstaticDir: {STATIC}\n"
            f"auth: {{viewerTokens: [view-token], operatorTokens: [{OPERATOR}]}}\n"
            "sources: [{type: demo, stepS: 0.4}]\n"
        )
        cls.backend = subprocess.Popen(
            [sys.executable, "-m", "cmu_backend", "--config", str(cfg), "--log-level", "warning"])
        url = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(url + "/api/v1/health", timeout=1)
                return url
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("backend se nespustil")

    def setUp(self) -> None:
        self.ctx = self.browser.new_context(viewport={"width": 1400, "height": 900})
        # webové fonty z internetu nejsou pro test potřeba (a v uzavřené síti nejsou dostupné)
        self.ctx.route(re.compile(r"https://fonts\.(googleapis|gstatic)\.com/.*"),
                       lambda r: r.fulfill(status=200, body="", content_type="text/css"))
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: m.type == "error" and self.errors.append(m.text))

    def tearDown(self) -> None:
        self.ctx.close()
        real = [e for e in self.errors if "401" not in e and "fonts.g" not in e]
        self.assertEqual(real, [], "chyby v konzoli prohlížeče")

    def shot(self, name: str, full: bool = True) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=f"{SHOTS}/{name}.png", full_page=full)

    def login(self, page: Page) -> None:
        page.goto(URL + "/#/")
        page.get_by_label("Token").fill(OPERATOR)
        page.get_by_role("button", name="Přihlásit").click()
        expect(page.locator(".tile").first).to_be_visible()

    def test_dashboard_tiles_are_chains(self) -> None:
        p = self.page
        p.goto(URL + "/#/")
        expect(p.get_by_role("heading", name="Přihlášení")).to_be_visible()
        p.get_by_label("Token").fill("spatny")
        p.get_by_role("button", name="Přihlásit").click()
        expect(p.get_by_text("Neplatný token")).to_be_visible()
        p.get_by_label("Token").fill(OPERATOR)
        p.get_by_role("button", name="Přihlásit").click()
        tiles = p.locator(".tile")
        expect(tiles.first).to_be_visible()
        self.assertGreaterEqual(tiles.count(), 10)
        # stejný řetězec na třech VM = tři dlaždice, každá s lokalitou
        orders = p.locator(".tile", has_text="Příjem objednávek")
        self.assertEqual(orders.count(), 4)
        expect(orders.filter(has_text="vm-prod-03")).to_contain_text("10.0.1.13")
        expect(tiles.first).to_contain_text("Problém")  # řazení: nejdřív problémy
        expect(p.locator(".banner")).to_contain_text("vm-test-02")
        expect(p.get_by_text("Operátor")).to_be_visible()
        self.shot("01-dashboard")

        chip = p.get_by_role("button", name=re.compile("Problém"))
        problems = int(chip.locator(".n").inner_text())
        self.assertGreaterEqual(problems, 1)
        chip.click()
        expect(p.locator(".tile")).to_have_count(problems)
        self.assertEqual(p.locator(".tile .pill", has_text="Problém").count(), problems)
        p.get_by_role("button", name="Zrušit filtry").click()
        p.get_by_placeholder(re.compile("Hledat")).fill("Notifikace")
        expect(p.locator(".tile")).to_have_count(1)
        expect(p.locator(".tile")).to_contain_text("docker-01")
        p.get_by_role("button", name="Zrušit filtry").click()
        p.get_by_label("Typ").select_option("container")
        expect(p.locator(".tile", has_text="Procesy")).to_have_count(0)
        self.assertGreaterEqual(p.locator(".tile", has_text="Kontejnery").count(), 3)
        self.assertIn("kind=container", p.url)

        p.set_viewport_size({"width": 390, "height": 844})
        p.get_by_role("button", name="Zrušit filtry").click()
        expect(p.locator(".tile").first).to_be_visible()
        overflow = p.evaluate("document.documentElement.scrollWidth > window.innerWidth")
        self.assertFalse(overflow, "na telefonu nesmí být vodorovný posun")
        self.shot("02-dashboard-phone", full=False)

    def test_chain_detail_and_actions(self) -> None:
        p = self.page
        self.login(p)
        p.locator(".tile", has_text="vm-prod-03").filter(has_text="Příjem objednávek").click()
        expect(p.get_by_role("heading", name="Příjem objednávek", level=1)).to_be_visible()
        expect(p.locator(".flow-node")).to_have_count(3)
        expect(p.locator(".flow-arrow.broken")).to_have_count(2)
        expect(p.get_by_text("Umístění")).to_be_visible()
        expect(p.locator(".logs")).to_contain_text("zpracována dávka")
        self.shot("03-detail-broken")
        p.get_by_role("button", name="Restartovat", exact=True).click()
        expect(p.locator(".op")).to_contain_text("probíhá")
        expect(p.locator(".op")).to_contain_text("hotovo", timeout=15000)
        expect(p.locator(".flow-arrow.broken")).to_have_count(0, timeout=10000)
        expect(p.locator(".pill").first).to_contain_text("V pořádku", timeout=10000)
        self.shot("04-detail-after-restart")

        p.goto(URL + "/#/")
        p.locator(".tile", has_text="vm-test-02").click()
        expect(p.get_by_text(re.compile("Lokalita neodpovídá"))).to_be_visible()
        for name in ("Spustit", "Restartovat", "Zastavit", "Odstranit"):
            expect(p.get_by_role("button", name=name, exact=True)).to_be_disabled()

    def test_new_container_chain(self) -> None:
        p = self.page
        self.login(p)
        p.get_by_role("link", name="Nový řetězec").click()
        p.get_by_label("Umístění").select_option(label="docker-02 · test")
        name = f"Export faktur {int(time.time()) % 100000}"
        p.get_by_label("Název řetězce").fill(name)
        images = p.get_by_label("Image")
        images.nth(0).fill("registry.local/collector:1.2.0")
        images.nth(1).fill("registry.local/packer")
        expect(p.locator(".checks")).to_contain_text("nemá tag")
        expect(p.locator(".preview pre")).to_contain_text("CHAIN_DOWNSTREAM_URL: http://app2:8080")
        p.get_by_role("button", name="Posunout app1 dolů").click()
        expect(p.locator(".preview pre")).to_contain_text("CHAIN_DOWNSTREAM_URL: http://app1:8080")
        self.shot("05-new-chain")
        p.get_by_role("button", name="Vytvořit a spustit").click()
        expect(p.locator(".op")).to_contain_text("hotovo", timeout=15000)
        p.get_by_role("link", name="Zobrazit řetězec").click()
        expect(p.locator(".tile", has_text=name)).to_contain_text("V pořádku")

    def test_sources_page(self) -> None:
        p = self.page
        self.login(p)
        p.get_by_role("link", name="Zdroje a integrace").click()
        expect(p.locator(".comp")).to_have_count(1)
        row = p.locator("tr", has_text="vm-test-02")
        expect(row).to_contain_text("Nedostupná")
        self.shot("06-sources")


if __name__ == "__main__":
    unittest.main()
