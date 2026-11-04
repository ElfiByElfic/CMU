import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { Chain, ChainApp, Operation } from "../api/types";
import { Badge, Empty, ErrorBox, HealthPill, Icon, OperationPanel } from "../components/ui";
import { ACTION_LABEL, CHAIN_STATE, KIND, SEVERITY, ago, appState, num, time } from "../lib/format";
import { navigate, useNow, useOperation, usePoll } from "../lib/hooks";

export function ChainDetail({ id, canOperate }: { id: string; canOperate: boolean }) {
  const chain = usePoll(() => api.chain(id), 3000, [id]);
  const now = useNow(2000);
  const [selected, setSelected] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [actionError, setActionError] = useState<Error | null>(null);
  const deleting = useOperation((op) => op.status === "succeeded" && navigate("/"));
  const ops = useOperation(() => void chain.reload());

  const c = chain.data;
  useEffect(() => {
    if (c && !selected && c.apps.length) setSelected(c.apps[0].id);
  }, [c, selected]);

  if (chain.error instanceof ApiError && chain.error.status === 404 && !deleting.op) {
    return <Empty>Řetězec neexistuje. <a href="#/">Zpět na přehled</a></Empty>;
  }
  if (!c) return chain.error ? <ErrorBox error={chain.error} /> : <Empty>Načítám…</Empty>;

  const busy = Boolean(c.busyOperation) || ops.op?.status === "running" || deleting.op?.status === "running";
  const can = (cap: string) => canOperate && !c.stale && !busy && c.capabilities.includes(cap);
  const run = async (fn: () => Promise<Operation>, tracker: typeof ops) => {
    setActionError(null);
    try {
      tracker.track(await fn());
    } catch (e) {
      setActionError(e as Error);
    }
  };
  const app = c.apps.find((a) => a.id === selected) ?? null;

  return (
    <>
      <nav className="crumbs" aria-label="Drobečková navigace">
        <a href="#/">Řetězce</a>
        <span aria-hidden="true">/</span>
        <span>{c.name}</span>
      </nav>

      <div className="page-head">
        <div>
          <div className="title-row">
            <h1>{c.name}</h1>
            <HealthPill health={c.health} />
            <Badge>{KIND[c.kind]}</Badge>
          </div>
          <p className="muted">
            {c.stale ? (
              <>Lokalita neodpovídá – poslední data {ago(c.lastSeen, now)}. Stav aplikací není znám.</>
            ) : (
              <>{CHAIN_STATE[c.state]} · data {ago(c.lastSeen, now)}</>
            )}
          </p>
        </div>
        {canOperate && (
          <div className="actions">
            {(["start", "restart", "stop"] as const).map((a) => (
              <button key={a} className={`btn ${a === "start" ? "green" : a === "stop" ? "red" : ""}`}
                disabled={!can(a)} onClick={() => run(() => api.chainAction(c.id, a), ops)}>
                {ACTION_LABEL[a]}
              </button>
            ))}
            <button className="btn outline-red" disabled={!can("delete")} onClick={() => setConfirmDelete(true)}>
              {ACTION_LABEL.delete}
            </button>
          </div>
        )}
      </div>

      {confirmDelete && (
        <div className="confirm" role="alertdialog" aria-label="Potvrzení odstranění">
          <span>
            Odstranit řetězec <strong>{c.name}</strong> z <span className="mono">{c.location.name}</span>?
            {c.kind === "container" ? " Kontejnery se odstraní a stack se smaže z Komoda." : " Agent ho zastaví odpředu a smaže jeho definici."}
          </span>
          <span className="actions">
            <button className="btn" onClick={() => setConfirmDelete(false)}>Zrušit</button>
            <button className="btn danger" onClick={() => {
              setConfirmDelete(false);
              void run(() => api.deleteChain(c.id), deleting);
            }}>Ano, odstranit</button>
          </span>
        </div>
      )}
      <ErrorBox error={actionError} />
      {ops.op && <OperationPanel op={ops.op} onClose={ops.clear} />}
      {deleting.op && <OperationPanel op={deleting.op} onClose={deleting.clear} />}

      <div className="cols">
        <div className="col-main">
          <section className="card">
            <h2>Aplikace v řetězci</h2>
            <p className="muted small">Pořadí = směr toku dat. Start probíhá odzadu, zastavení odpředu.</p>
            <div className="flow">
              {c.apps.map((a, i) => (
                <FlowNode key={a.id} app={a} first={i === 0} prev={c.apps[i - 1]} selected={a.id === selected}
                  onSelect={() => setSelected(a.id)} />
              ))}
            </div>
          </section>

          {app && (
            <AppPanel chain={c} app={app} canOperate={canOperate && !busy}
              onAction={(action) => run(() => api.appAction(c.id, app.id, action), ops)} />
          )}
        </div>

        <aside className="col-side">
          <section className="card">
            <h2>Umístění</h2>
            <dl className="kv">
              <dt>Lokalita</dt><dd className="mono">{c.location.name}</dd>
              <dt>Adresa</dt><dd className="mono">{c.location.address ?? "–"}</dd>
              <dt>Prostředí</dt><dd>{c.location.environment ?? "–"}</dd>
              <dt>Typ</dt><dd>{c.location.kind === "docker-host" ? "Docker server (Komodo)" : "VM s agentem"}</dd>
              <dt>Zdroj</dt><dd className="mono">{c.source.id}</dd>
              <dt>ID ve zdroji</dt><dd className="mono">{c.localId}</dd>
              {c.desiredRunning !== null && (<><dt>Má běžet</dt><dd>{c.desiredRunning ? "ano" : "ne"}</dd></>)}
            </dl>
            <a className="small" href={`#/?location=${encodeURIComponent(c.location.name)}`}>
              Další řetězce na {c.location.name}
            </a>
          </section>

          <section className="card">
            <h2>Výstrahy {c.alerts.length > 0 && <span className="count">{c.alerts.length}</span>}</h2>
            {c.alerts.length === 0 ? (
              <p className="muted small">Žádné aktivní výstrahy.</p>
            ) : (
              <ul className="alerts">
                {c.alerts.map((al, i) => (
                  <li key={`${al.name}-${i}`}>
                    <span className={`sev ${SEVERITY[al.severity].cls}`}>{SEVERITY[al.severity].label}</span>
                    <span>
                      {al.url ? <a href={al.url} target="_blank" rel="noreferrer">{al.name} <Icon name="link" size={12} /></a> : al.name}
                      <span className="muted small block">{al.source} · {al.host} · od {time(al.since)}</span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {c.metrics.length > 0 && (
            <section className="card">
              <h2>Metriky</h2>
              <div className="metrics">
                {c.metrics.map((m) => (
                  <div key={m.id} className="metric">
                    <span className="muted small">{m.label}</span>
                    <span className="mono big">{num(m.value, m.unit)}</span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </aside>
      </div>
    </>
  );
}

function FlowNode({ app, first, prev, selected, onSelect }: {
  app: ChainApp; first: boolean; prev?: ChainApp; selected: boolean; onSelect: () => void;
}) {
  const st = appState(app.state);
  const alive = (a?: ChainApp) => a && ["running", "degraded", "paused"].includes(a.state);
  const linkOk = first || (alive(prev) && alive(app));
  return (
    <>
      {!first && (
        <span className={`flow-arrow ${linkOk ? "" : "broken"}`} aria-hidden="true">
          <svg width="36" height="18" viewBox="0 0 36 18" fill="none" stroke="currentColor" strokeWidth="2"
            strokeLinecap="round" strokeDasharray={linkOk ? undefined : "4 4"}>
            <path d="M2 9h28M24 3l6 6-6 6" />
          </svg>
        </span>
      )}
      <button className={`flow-node ${st.cls} ${selected ? "selected" : ""}`} onClick={onSelect}
        aria-pressed={selected} aria-label={`${app.name}: ${st.label}`}>
        <span className="row">
          <strong>{app.name}</strong>
          <span className={`state ${st.cls}`}>{st.label}</span>
        </span>
        <span className="mono muted small ellipsis">
          {app.image ?? `v${app.version ?? "?"}`}
          {app.port ? ` · :${app.port}` : ""}
        </span>
      </button>
    </>
  );
}

function AppPanel({ chain, app, canOperate, onAction }: {
  chain: Chain; app: ChainApp; canOperate: boolean; onAction: (action: string) => void;
}) {
  const [lines, setLines] = useState<string[] | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const hasLogs = chain.capabilities.includes("logs") && !chain.stale;
  const load = async () => {
    setError(null);
    try {
      setLines((await api.appLogs(chain.id, app.id, 200)).lines);
    } catch (e) {
      setError(e as Error);
    }
  };
  useEffect(() => {
    setLines(null);
    if (hasLogs) void load();
    // načíst při změně aplikace
  }, [chain.id, app.id]);

  const st = appState(app.state);
  const actions = ["start", "restart", "stop", "reload"].filter((a) => chain.capabilities.includes(`app:${a}`));
  const running = ["running", "degraded", "paused"].includes(app.state);
  return (
    <section className="card">
      <div className="card-head">
        <h2>{app.name} <span className={`state ${st.cls}`}>{st.label}</span></h2>
        {canOperate && (
          <div className="actions">
            {actions.map((a) => (
              <button key={a} className="btn small" disabled={chain.stale || (a === "start" ? running : !running)}
                onClick={() => onAction(a)} aria-label={`${ACTION_LABEL[a]} ${app.name}`}>
                {ACTION_LABEL[a]}
              </button>
            ))}
          </div>
        )}
      </div>
      <dl className="kv cols-2">
        <dt>{app.image ? "Image" : "Verze"}</dt><dd className="mono">{app.image ?? app.version ?? "–"}</dd>
        <dt>Pozice</dt><dd>{app.position === 0 ? "vstup" : app.position === chain.apps.length - 1 ? "výstup" : `${app.position + 1}.`}</dd>
        {app.port !== null && (<><dt>Port</dt><dd className="mono">{app.port}</dd></>)}
        {app.ready !== null && (<><dt>Připravena</dt><dd>{app.ready ? "ano" : "ne"}</dd></>)}
        {app.restarts !== null && (<><dt>Restarty</dt><dd>{app.restarts}</dd></>)}
        {app.configPending && (<><dt>Konfigurace</dt><dd className="s-warning">změněna – je potřeba reload/restart</dd></>)}
      </dl>
      {hasLogs && (
        <>
          <div className="card-head">
            <h3>Logy</h3>
            <button className="icon-btn" onClick={() => void load()} aria-label="Obnovit logy">
              <Icon name="refresh" />
            </button>
          </div>
          <ErrorBox error={error} />
          <pre className="logs">{lines === null ? "Načítám…" : lines.length ? lines.join("\n") : "Žádné záznamy."}</pre>
        </>
      )}
    </section>
  );
}
