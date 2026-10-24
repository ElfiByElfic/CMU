# CMU – Centrální mozek

Správa řetězců aplikací.

Systém pro provoz a ovládání řetězců aplikací (např. App1 → App2 → App3), které běží na více
virtuálních strojích v jedné síti. Každá VM má **agenta**, který řetězce spouští, hlídá a ovládá;
každá aplikace poskytuje **povinné REST API**; nad tím stojí centrální backend a webové GUI
(dashboard VM → řetězce → ovládání aplikace).

Stav: návrh API + první implementace agenta a knihovny pro aplikace. Centrální backend zatím
neexistuje, GUI je klikací prototyp v `gui-prototype/index.html` (viz kapitola 8).

## Obsah repozitáře

```
api/
  app-api.yaml          OpenAPI – povinné API každé aplikace
  agent-api.yaml        OpenAPI – API agenta na VM
libchainapp/            Qt knihovna implementující povinné API aplikace
  include/chainapp/     veřejné rozhraní (chainapp.h)
  src/                  implementace
  example/main.cpp      ukázková aplikace – vzor přizpůsobení
  chainapp.pri          include pro projekty stavěné qmake
agent/                  chain-agent (Qt)
  src/                  model, katalog, instance, správa řetězců, REST server
  config/               ukázková konfigurace agenta
  systemd/              unit soubor
.github/workflows/      CI: validace OpenAPI + build knihovny a agenta
gui-prototype/
  index.html            klikací prototyp webového GUI (jeden soubor, otevřít v prohlížeči)
```

## Sestavení

Požadavky: Qt ≥ 6.4 s moduly **Core, Network, HttpServer**, C++17, CMake ≥ 3.21.

```bash
# knihovna + ukázková aplikace
cmake -S libchainapp -B build/libchainapp -DCMAKE_PREFIX_PATH=/cesta/k/Qt/6.x/gcc_64
cmake --build build/libchainapp

# agent
cmake -S agent -B build/agent -DCMAKE_PREFIX_PATH=/cesta/k/Qt/6.x/gcc_64
cmake --build build/agent
```

Projekty stavěné přes qmake přidají knihovnu řádkem `include(/cesta/k/libchainapp/chainapp.pri)`.

## Rychlé vyzkoušení na jednom stroji

```bash
# 1) katalog s ukázkovou aplikací pod dvěma "typy"
for T in App1 App2; do
  mkdir -p /tmp/chain-apps/$T/1.0.0/bin
  cp build/libchainapp/example-app /tmp/chain-apps/$T/1.0.0/bin/$T
  echo "{\"type\":\"$T\",\"version\":\"1.0.0\",\"executable\":\"bin/$T\"}" > /tmp/chain-apps/$T/1.0.0/manifest.json
done

# 2) konfigurace agenta
sed -e 's#/opt/chain-apps#/tmp/chain-apps#' \
    -e 's#/var/lib/chain-agent#/tmp/chain-agent/state#' \
    -e 's#/var/log/chain-agent#/tmp/chain-agent/log#' \
    agent/config/agent.example.json > /tmp/agent.json
./build/agent/chain-agent --config /tmp/agent.json &

# 3) vytvořit a spustit řetězec App1 → App2
TOKEN=ZMENTE-control-token
curl -s -X POST "http://localhost:9100/agent/v1/chains?start=true" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Test","apps":[{"type":"App1","version":"1.0.0"},{"type":"App2","version":"1.0.0"}]}'

# 4) stav
curl -s http://localhost:9100/agent/v1/overview -H "Authorization: Bearer $TOKEN"
```

Ukázková aplikace se vždy hlásí jako `App2 2.4.1` (viz `example/main.cpp`); pro test to nevadí.

## Architektura

```
 Web GUI ──► centrální backend ──► chain-agent (VM 1) ──► App1 ─► App2 ─► App3
                                │        :9100            127.0.0.1:85xx
                                └──► chain-agent (VM 2) ──► …
```

## 1. Kdo s kým mluví

- **Backend → agent**: REST `/agent/v1/*` na portu 9100, Bearer token s rolí `read` nebo `control`.
- **Agent → aplikace**: REST `/api/v1/*` na `127.0.0.1:<port>`. Aplikace naslouchá **jen na localhostu**,
  zvenku dostupná není. Každá instance má vlastní token, který jí agent předá při startu.
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
| `APP_API_TOKEN` | `…` | Token, kterým se agent prokazuje |
| `APP_CONFIG_FILE` | `…/config.json` | Konfigurace z definice řetězce |
| `APP_DATA_DIR` | `/var/lib/chain-agent/instances/chain-a.app2` | Vlastní adresář instance (pracovní adresář) |

Stdout a stderr aplikace zapisuje agent do `/var/log/chain-agent/<chain>.<app>.log`, odtud je čte `GET /apps/{id}/logs`.

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

## 4. Jak přizpůsobit aplikaci

Celý postup je v `libchainapp/example/main.cpp`. Stručně:

1. Přilinkovat `libchainapp` (CMake `add_subdirectory` + `target_link_libraries(app PRIVATE chainapp)`,
   nebo qmake `include(chainapp.pri)`).
2. Vytvořit `ChainApp chain("App2", APP_VERSION, GIT_SHA);` – verzi a commit předejte z GitLab CI přes `-D`.
3. Zaregistrovat konfiguraci `addConfig(...)` a kontroly `addReadinessCheck(...)`.
4. Připojit `drainRequested` (přestat přijímat, dokončit, zavolat `drainFinished()`), případně `pause/resume/reload`.
5. **Odstranit z kódu napevno zadané porty, URL a cesty** – brát je z `chain.identity()` a `chain.config()`.
6. Po inicializaci zavolat `chain.setState(State::Running)`; při problému `Degraded` nebo `Error`.
7. Logovat na stdout/stderr, ne do vlastních souborů s pevnou cestou.
8. Zápis dat jen do `APP_DATA_DIR` – na VM běží víc instancí stejné aplikace.

Proces se má na `stop` ukončit s kódem 0 (knihovna to udělá po `drainFinished()`).
Jiný kód nebo pád = agent ho podle `restartPolicy` restartuje s backoffem 2–60 s.

## 5. Životní cyklus řetězce v agentovi

- **Start**: odzadu (App3 → App2 → App1). Každou instanci spustí, čeká na `/health/ready` = 200
  (max `readyTimeoutMs`), teprve pak další. Selhání = operace `failed`, řetězec `broken`.
- **Stop**: odpředu (App1 → App2 → App3). `POST /control/stop` → po `stopTimeoutMs` SIGTERM → po 5 s SIGKILL.
- **Úprava** (`PUT /chains/{id}`): stop → nové instance podle nové definice → start (pokud měl běžet).
- **Požadovaný stav** se ukládá do `/var/lib/chain-agent/chains.json`; po restartu agenta se řetězce
  s `desiredRunning=true` a `autostart=true` znovu spustí.
- Nad jedním řetězcem běží vždy jen jedna operace (další dostane `409`).

## 6. Katalog a nasazení verzí

```
/opt/chain-apps/App2/2.4.1/manifest.json   {"type":"App2","version":"2.4.1","executable":"bin/App2","args":[]}
/opt/chain-apps/App2/2.4.1/bin/App2
```

Doporučení pro GitLab CI: job po buildu publikuje balík do Package Registry a deploy job ho rozbalí
do `/opt/chain-apps/<Type>/<version>/` na cílových VM. Agent katalog jen čte (`GET /catalog` ho přeskenuje),
takže nasazení nové verze nic nespouští – použije se až při vytvoření nebo úpravě řetězce.

## 7. Známá omezení a další kroky

- Aplikace běží jako potomci agenta (`QProcess`): zastavení agenta zastaví i aplikace.
  Pokud mají restart agenta přežít, nahraďte v `AppInstance::start/stop` spouštění procesu voláním
  `systemctl start chain-app@<instance>.service` (systemd šablona) – API agenta se nemění.
- Rotaci logů řešte `logrotate` (`copytruncate`) nad `/var/log/chain-agent/*.log`.
- Komunikace backend ↔ agent je HTTP; mimo izolovanou síť přidejte TLS (reverse proxy nebo `QSslServer`).
- Kód zatím nebyl ověřen buildem – výsledek prvního běhu CI (`.github/workflows/build.yml`) ukáže, zda jsou potřeba úpravy pro danou verzi Qt.

## 8. Web GUI (návrh)

Klikací prototyp je v `gui-prototype/index.html` – jeden soubor bez buildu, stačí otevřít
v prohlížeči. Data jsou ukázková a celé chování (spouštění odzadu, stavy, validace) je simulované
v prohlížeči, žádný backend se nevolá. Obsahuje:

- **Dashboard** – dlaždice VM se souhrnným stavem (V pořádku / Varování / Problém / Nedostupná)
  a miniaturou stavů aplikací v každém řetězci.
- **Detail VM** – řetězce jako řady aplikací se šipkami; přerušený spoj je čárkovaný.
  Akce *Přidat řetězec*, *Spustit / Zastavit řetězec*, *Odstranit*.
- **Průvodce novým řetězcem** – název, výběr aplikací z katalogu, pořadí, verze, náhled
  s automaticky přidělenými porty a validací (odpovídá `POST /agent/v1/validate` a `POST /agent/v1/chains`).
- **Ovládání aplikace** – Spustit / Restartovat / Zastavit, metriky, návaznost v řetězci,
  poslední odpovědi povinných endpointů, logy a změna úrovně logování.

Data pro GUI dodává centrální backend, který periodicky čte `GET /agent/v1/overview` ze všech agentů
a změny posílá do prohlížeče přes SSE nebo WebSocket. Agent nedostupný déle než 3 intervaly se
zobrazuje šedě jako *Nedostupná*, nikdy s posledním známým stavem.
