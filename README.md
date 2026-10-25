# CMU – Centrální mozek

Správa řetězců aplikací.

Systém pro provoz a ovládání řetězců aplikací (např. App1 → App2 → App3), které běží na desítkách
až stovkách virtuálních strojů v jedné síti. Každá VM má **agenta**, který řetězce spouští, hlídá,
ovládá a rozdává aplikacím jejich **konfiguraci**. Každá aplikace poskytuje **povinné REST API**.
Nad tím stojí centrální backend a webové GUI (dashboard VM → řetězce → ovládání aplikace).

Stav:

| Část | Stav |
|---|---|
| Povinné API aplikací (`api/app-api.yaml`) | návrh hotový |
| `libchainapp` – Qt knihovna s povinným API | implementace, build ověřený v CI |
| `agent/` – chain-agent (Python, FastAPI) | implementace, end-to-end testy se skutečnými procesy |
| Centrální backend + GUI | zatím jen klikací prototyp (`gui-prototype/`), plánováno ve Flasku (kap. 10) |

## Obsah repozitáře

```
api/
  app-api.yaml          OpenAPI – povinné API každé aplikace
  agent-api.yaml        OpenAPI – API agenta na VM (testy hlídají shodu s implementací)
agent/                  chain-agent (Python ≥ 3.10, FastAPI)
  chain_agent/          model, katalog, konfigurace aplikací, instance, správa řetězců, REST API
  tests/                end-to-end testy + fake_app.py (referenční aplikace s povinným API)
  config/               ukázková konfigurace agenta
  systemd/              unit soubor
libchainapp/            Qt knihovna implementující povinné API aplikace
  include/chainapp/     veřejné rozhraní (chainapp.h)
  src/                  implementace
  example/main.cpp      ukázková aplikace – vzor přizpůsobení
  chainapp.pri          include pro projekty stavěné qmake
gui-prototype/
  index.html            klikací prototyp webového GUI (jeden soubor, otevřít v prohlížeči)
.github/workflows/      CI: validace OpenAPI, testy agenta, build libchainapp
```

## Instalace agenta

Požadavky: Python ≥ 3.10 na každé VM.

```bash
sudo useradd --system --home /var/lib/chain-agent chainagent
sudo python3 -m venv /opt/chain-agent/venv
sudo /opt/chain-agent/venv/bin/pip install ./agent          # nebo wheel z CI
sudo install -D -m 600 -o chainagent agent/config/agent.example.json /etc/chain-agent/agent.json
sudo install -m 644 agent/systemd/chain-agent.service /etc/systemd/system/
sudo systemctl enable --now chain-agent
```

V `/etc/chain-agent/agent.json` je potřeba nastavit `vmName` a **vlastní tokeny** (`readTokens`,
`controlTokens`). Živá dokumentace API běžícího agenta je na `http://<vm>:9100/docs`.

| Klíč | Výchozí | Význam |
|---|---|---|
| `vmName` | – | Jméno VM v ID instancí a v GUI |
| `environment` | `production` | Na produkci agent varuje před předběžnými verzemi (`-rc1`) |
| `listenAddress` / `listenPort` | `0.0.0.0` / `9100` | Kde agent naslouchá |
| `readTokens` / `controlTokens` | – | Bearer tokeny pro roli `read` (jen čtení) a `control` (vše) |
| `catalogDir` | `/opt/chain-apps` | Katalog verzí aplikací (kap. 8) |
| `stateDir` / `logDir` | `/var/lib/chain-agent` / `/var/log/chain-agent` | Stav (`chains.json`, data instancí) a logy |
| `envConfigDir` | – | Konfigurace pro prostředí: `<Type>.json` nebo `<Type>.ini` (kap. 6) |
| `secretsDir` | `/etc/chain-agent/secrets` | Tajné hodnoty, jeden soubor na hodnotu (kap. 6) |
| `portRange` | `[8500, 8999]` | Z tohoto rozsahu agent přiděluje porty aplikacím |
| `pollIntervalMs` | `3000` | Jak často se agent ptá aplikací na stav |
| `readyTimeoutMs` / `stopTimeoutMs` | `60000` / `30000` | Limity pro start (ready) a řízené zastavení |
| `restartBackoffMaxS` | `60` | Strop prodlevy mezi restarty spadlé aplikace |

## Rychlé vyzkoušení na jednom stroji

Bez Qt, s referenční aplikací `agent/tests/fake_app.py`:

```bash
cd agent && python3 -m venv .venv && . .venv/bin/activate && pip install -e .

# 1) katalog: App1 (konfigurace INI) a App2 (JSON)
for T in App1 App2; do
  D=/tmp/chain-apps/$T/1.0.0; mkdir -p $D/bin
  printf '#!/bin/sh\nexec %s %s "$@"\n' "$(which python3)" "$PWD/tests/fake_app.py" > $D/bin/$T && chmod +x $D/bin/$T
done
echo '{"type":"App1","version":"1.0.0","executable":"bin/App1","configFormat":"ini"}' > /tmp/chain-apps/App1/1.0.0/manifest.json
echo '{"type":"App2","version":"1.0.0","executable":"bin/App2"}'                      > /tmp/chain-apps/App2/1.0.0/manifest.json

# 2) agent
cat > /tmp/agent.json <<'EOF'
{"vmName":"vm-local","controlTokens":["t"],"catalogDir":"/tmp/chain-apps",
 "stateDir":"/tmp/chain-agent/state","logDir":"/tmp/chain-agent/log","secretsDir":"/tmp/chain-agent/secrets"}
EOF
chain-agent --config /tmp/agent.json &

# 3) vytvořit a spustit řetězec App1 → App2, pak stav a konfigurace App1
curl -s -X POST "localhost:9100/agent/v1/chains?start=true" -H "Authorization: Bearer t" \
  -H "Content-Type: application/json" \
  -d '{"id":"test","name":"Test","apps":[{"type":"App1","version":"1.0.0","config":{"logLevel":"DEBUG"}},{"type":"App2","version":"1.0.0"}]}'
curl -s localhost:9100/agent/v1/overview -H "Authorization: Bearer t"
curl -s localhost:9100/agent/v1/apps/test.app1/config -H "Authorization: Bearer t"
```

## Architektura

```
 prohlížeč ──► backend + GUI (Flask) ◄── DB/Redis ◄── kolektor ──► chain-agent (VM 1) ──► App1 ─► App2 ─► App3
                                                          │          :9100              127.0.0.1:85xx
                                                          └──────► chain-agent (VM 2…N)
```

## 1. Kdo s kým mluví

- **Kolektor/backend → agent**: REST `/agent/v1/*` na portu 9100, Bearer token s rolí `read` nebo `control`.
  Celý stav VM vrací jeden dotaz `GET /agent/v1/overview`.
- **Agent → aplikace**: REST `/api/v1/*` na `127.0.0.1:<port>`. Aplikace naslouchá **jen na localhostu**,
  zvenku dostupná není. Každá instance má vlastní token (`APP_API_TOKEN`), který jí agent předá při startu.
- **Aplikace → agent**: stažení vlastní konfigurace `GET 127.0.0.1:9100/agent/v1/apps/<id>/config`
  stejným tokenem (kap. 6).
- **Aplikace → aplikace** (tok dat v řetězci): jak jste zvyklí; agent jen předá URL sousedů.
- **Spuštění procesu** nedělá REST API aplikace (když neběží, nemá kdo odpovědět) – proces spouští agent.

## 2. Spouštěcí kontrakt (agent → aplikace)

Agent spouští aplikaci s těmito proměnnými prostředí. `libchainapp` je čte sama.

| Proměnná | Příklad | Význam |
|---|---|---|
| `CHAIN_INSTANCE_ID` | `vm-prod-02/chain-a/app2` | Globálně unikátní ID instance |
| `CHAIN_ID` | `chain-a` | Řetězec |
| `CHAIN_POSITION` | `1` | Pozice v řetězci, 0 = vstup |
| `CHAIN_UPSTREAM_URL` | `http://127.0.0.1:8500` | Předchozí aplikace (prázdné u vstupu) |
| `CHAIN_DOWNSTREAM_URL` | `http://127.0.0.1:8502` | Další aplikace (prázdné u výstupu) |
| `APP_BIND_ADDRESS` | `127.0.0.1` | Kde naslouchat |
| `APP_PORT` | `8501` | Port přidělený agentem |
| `APP_API_TOKEN` | `…` | Společný token agent ↔ instance (oběma směry) |
| `APP_CONFIG_FILE` | `…/config.ini` | Konfigurace připravená agentem (práva 600) |
| `APP_CONFIG_FORMAT` | `ini` | Formát souboru: `ini` nebo `json` (z manifestu) |
| `APP_CONFIG_URL` | `http://127.0.0.1:9100/agent/v1/apps/chain-a.app2/config` | Odkud si aplikace konfiguraci stáhne |
| `APP_DATA_DIR` | `/var/lib/chain-agent/instances/chain-a.app2` | Vlastní adresář instance (pracovní adresář) |

Stdout a stderr aplikace zapisuje agent do `/var/log/chain-agent/<chain>.<app>.log`, odtud je čte
`GET /apps/{id}/logs`.

## 3. Povinné API aplikace (shrnutí `api/app-api.yaml`)

| Endpoint | K čemu ho agent používá |
|---|---|
| `GET /api/v1/health/live` | Žije proces? |
| `GET /api/v1/health/ready` | 200 = připravena, 503 = ne (s výpisem kontrol). Řídí postupný start řetězce. |
| `GET /api/v1/info` | Identita: typ, verze, build, řetězec, pozice, sousedé, PID |
| `GET /api/v1/status` | Stav (`starting/running/degraded/paused/draining/stopping/error`), uptime, zpracováno, chyby, fronta |
| `GET /api/v1/metrics` | Prometheus metriky |
| `GET` / `PATCH /api/v1/config` | Konfigurace; za běhu jen klíče `runtimeEditable` |
| `POST /api/v1/control/{drain\|stop\|pause\|resume\|reload}` | Řízení, vrací `202` |

Společná pravidla: prefix `/api/v1`, časy UTC ISO 8601, chyby `application/problem+json`,
hlavička `X-Correlation-ID` se přebírá a posílá dál po řetězci.
Referenční implementace v Pythonu (jen standardní knihovna) je `agent/tests/fake_app.py`.

## 4. API agenta (shrnutí `api/agent-api.yaml`)

| Endpoint | Účel |
|---|---|
| `GET /overview` | Celý stav VM, řetězců a instancí jedním dotazem (pro kolektor) |
| `GET /catalog` | Dostupné aplikace a verze |
| `POST /validate` | Kontrola definice řetězce a přidělení portů bez provedení změn |
| `GET/POST /chains`, `GET/PUT/DELETE /chains/{id}` | Správa řetězců |
| `POST /chains/{id}/{start\|stop\|restart}` | Ovládání celého řetězce |
| `GET /apps/{id}`, `POST /apps/{id}/{start\|stop\|restart\|drain\|pause\|resume\|reload}` | Ovládání instance |
| `GET/PATCH /apps/{id}/config` | Konfigurace instance (kap. 6) |
| `GET /apps/{id}/logs?tail=N` | Konec logu instance |
| `GET /operations/{id}` | Průběh asynchronní operace krok po kroku |

Dlouhé akce vrací `202` s operací. Nad jedním řetězcem běží vždy jen jedna operace (další dostane `409`).
Každá změna se zapisuje do `audit.log` (token jen jako otisk, nikdy v čitelné podobě).

## 5. Jak přizpůsobit aplikaci

Celý postup je v `libchainapp/example/main.cpp`. Stručně:

1. Přilinkovat `libchainapp` (CMake `add_subdirectory` + `target_link_libraries(app PRIVATE chainapp)`,
   nebo qmake `include(chainapp.pri)`).
2. Vytvořit `ChainApp chain("App2", APP_VERSION, GIT_SHA);` – verzi a commit předejte z CI přes `-D`.
3. Zaregistrovat konfiguraci `addConfig(...)` a kontroly `addReadinessCheck(...)`.
4. Připojit `drainRequested` (přestat přijímat, dokončit, zavolat `drainFinished()`), případně `pause/resume/reload`.
5. **Odstranit z kódu napevno zadané porty, URL a cesty** – brát je z `chain.identity()` a `chain.config()`.
6. Po inicializaci zavolat `chain.setState(State::Running)`; při problému `Degraded` nebo `Error`.
7. Logovat na stdout/stderr, ne do vlastních souborů s pevnou cestou.
8. Zápis dat jen do `APP_DATA_DIR` – na VM běží víc instancí stejné aplikace.

Proces se má na `stop` ukončit s kódem 0 (knihovna to udělá po `drainFinished()`).

## 6. Konfigurace aplikací

Každá aplikace dostane svou konfiguraci jako **INI nebo JSON**, podle `configFormat` v manifestu.

**Jak se k ní dostane** – dvě rovnocenné cesty, aplikace si vybere:

1. **Soubor** `APP_CONFIG_FILE` – agent ho zapíše před každým startem a před `reload`.
   Pro Qt aplikace stačí `QSettings(path, QSettings::IniFormat)`, bez dalšího kódu.
2. **Stažení** `GET APP_CONFIG_URL` s hlavičkou `Authorization: Bearer $APP_API_TOKEN`.
   Odpověď nese `ETag`; s `If-None-Match` vrací agent `304`, pokud se nic nezměnilo.
   Token instance platí jen pro její vlastní konfiguraci.

**Vrstvy** (pozdější přepisují dřívější):

| # | Vrstva | Kde žije | Kdo mění |
|---|---|---|---|
| 1 | Výchozí | `defaultConfig` ve verzi aplikace v katalogu | vývojář, s novou verzí |
| 2 | Prostředí | `envConfigDir/<Type>.json\|.ini` na VM | správce VM / deploy |
| 3 | Řetězec | `config` v definici řetězce (`chains.json`) | GUI přes `PATCH /apps/{id}/config` |
| 4 | Runtime | sekce `chain` | agent (port, sousedé, ID) – nejde přepsat |

Příklad výsledku ve formátu INI (top-level hodnoty v sekci `[General]`, kterou QSettings čte jako kořen):

```ini
[General]
logLevel=DEBUG

[chain]
chainId=chain-a
downstreamUrl=http://127.0.0.1:8502
instanceId=vm-prod-02/chain-a/app2
port=8501
position=1
upstreamUrl=http://127.0.0.1:8500

[database]
host=db1
password=…
```

**Změna z GUI** – `PATCH /apps/{id}/config` s JSON Merge Patch (`null` klíč maže a vrací výchozí hodnotu).
Agent změnu uloží, přepíše soubor a pošle ji běžící aplikaci (`PATCH /api/v1/config`).
Když ji aplikace za běhu nepřijme, odpověď obsahuje `restartRequired: true`, instance má
`configPending: true` a VM je v GUI ve stavu *Varování*, dokud nedojde k `reload` nebo restartu.

**Tajné hodnoty** – do konfigurace se píše jen odkaz `secret://db-password`; agent dosadí obsah
souboru `secretsDir/db-password` jen do souboru aplikace a do odpovědi s tokenem instance.
Pro role `read`/`control` (GUI) je hodnota zamaskovaná `***`. Chybějící tajná hodnota = chyba validace.

## 7. Životní cyklus řetězce v agentovi

- **Start**: odzadu (App3 → App2 → App1). Každou instanci spustí, čeká na `/health/ready` = 200
  (max `readyTimeoutMs`), teprve pak další. Selhání = operace `failed`, řetězec `broken`.
- **Stop**: odpředu (App1 → App2 → App3). `POST /control/stop` → po `stopTimeoutMs` SIGTERM → po 5 s SIGKILL.
- **Úprava** (`PUT /chains/{id}`): stop → nové instance podle nové definice → start (pokud měl běžet).
- **Pád**: podle `restartPolicy` (`never`, `on-failure` = jen nenulový kód, `always`) s backoffem
  2, 4, 8 … max `restartBackoffMaxS` s; po minutě stabilního běhu se počítadlo pádů nuluje.
- **Požadovaný stav** se ukládá do `stateDir/chains.json`; po restartu agenta se řetězce
  s `desiredRunning=true` a `autostart=true` znovu spustí.
- Porty přiděluje agent z `portRange` a přeskakuje porty, které na VM už něco používá.

## 8. Katalog a nasazení verzí

```
/opt/chain-apps/App2/2.4.1/manifest.json
/opt/chain-apps/App2/2.4.1/bin/App2
/opt/chain-apps/App2/2.4.1/default.ini
```

```json
{"type": "App2", "version": "2.4.1", "executable": "bin/App2", "args": [],
 "configFormat": "ini", "defaultConfig": "default.ini"}
```

Doporučení pro CI: job po buildu publikuje balík do Package Registry a deploy job ho rozbalí
do `/opt/chain-apps/<Type>/<version>/` na cílových VM. Agent katalog jen čte (`GET /catalog` ho přeskenuje),
takže nasazení nové verze nic nespouští – použije se až při vytvoření nebo úpravě řetězce.

## 9. Vývoj a testy

```bash
cd agent && pip install -e '.[dev]'
pytest -v          # end-to-end testy spouštějí skutečné procesy tests/fake_app.py
ruff check .
```

Testy pokrývají: role a tokeny, validaci, spouštění odzadu a zastavování odpředu, konflikt operací,
stahování konfigurace (INI/JSON, ETag, tajné hodnoty), změnu konfigurace za běhu i s restartem,
restart po pádu, selhání startu, SIGTERM po timeoutu, perzistenci a autostart po restartu agenta
a **shodu `api/agent-api.yaml` s implementací**.

## 10. Centrální backend a GUI (plán)

Pro stovky VM je klíčové **oddělit sběr stavu od webu**:

- **Kolektor** (samostatný proces, `asyncio` + `httpx`) se každých pár sekund zeptá všech agentů na
  `GET /overview` a uloží výsledek do sdíleného úložiště (Redis / PostgreSQL / SQLite).
  300 VM × 1 dotaz / 5 s ≈ 60 požadavků za sekundu – malá zátěž.
- **Web + GUI ve Flasku** čte jen hotová data z úložiště, nikdy se při requestu neptá agentů.
  Stránky se obnovují přes htmx; akce (start, stop, konfigurace) Flask přeposílá na agenta.
- Dashboard pro stovky VM: souhrn, filtr podle stavu (nejdřív problémy), seskupení podle prostředí;
  detail VM až po kliknutí. Agent nedostupný déle než 3 intervaly je šedý *Nedostupná*,
  nikdy s posledním známým stavem.

Klikací prototyp GUI je v `gui-prototype/index.html` – jeden soubor bez buildu, stačí otevřít
v prohlížeči. Data jsou ukázková a chování je simulované v prohlížeči. Obsahuje dashboard VM,
detail VM s řetězci, průvodce novým řetězcem (odpovídá `POST /validate` a `POST /chains`)
a ovládání aplikace.

## 11. Známá omezení a další kroky

- Aplikace jsou potomci agenta: zastavení agenta (systemd `KillMode=control-group`) zastaví i aplikace,
  po startu je agent spustí znovu. Pokud mají restart agenta přežít, lze spouštění v `instance.py`
  nahradit systemd šablonou `chain-app@<instance>.service` – API agenta se nemění.
- `libchainapp` zatím čte konfiguraci jen ze souboru (`APP_CONFIG_FILE`, JSON); stahování přes
  `APP_CONFIG_URL` a INI jsou v ní dalším krokem.
- Rotaci logů řešte `logrotate` (`copytruncate`) nad `/var/log/chain-agent/*.log`.
- Komunikace backend ↔ agent je HTTP; mimo izolovanou síť přidejte TLS (reverse proxy, nebo
  `uvicorn --ssl-keyfile/--ssl-certfile`).
- Zdroj pravdy pro konfiguraci (databáze backendu, nebo Git repozitář s review) zatím není rozhodnutý.
