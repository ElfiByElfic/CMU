import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { CatalogEntry, ChainCreate, ChainValidation, ContainerAppSpec, ProcessAppSpec, Target } from "../api/types";
import { Empty, ErrorBox, Icon, OperationPanel } from "../components/ui";
import { useDebounced, useOperation, usePoll } from "../lib/hooks";

interface ContainerRow extends ContainerAppSpec {
  envText: string;
}

const NEW_CONTAINER = (n: number): ContainerRow => ({
  name: `app${n}`, image: "", port: 8080, env: {}, envText: "", publish: null, healthcheck: true,
});

function parseEnv(text: string): Record<string, string> {
  const env: Record<string, string> = {};
  text.split("\n").forEach((line) => {
    const t = line.trim();
    if (!t || t.startsWith("#")) return;
    const i = t.indexOf("=");
    if (i > 0) env[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  });
  return env;
}

function move<T>(list: T[], i: number, d: number): T[] {
  const next = [...list];
  [next[i], next[i + d]] = [next[i + d], next[i]];
  return next;
}

export function NewChain() {
  const targets = usePoll(api.targets, 15000);
  const [targetKey, setTargetKey] = useState("");
  const [name, setName] = useState("");
  const [proc, setProc] = useState<ProcessAppSpec[]>([]);
  const [cont, setCont] = useState<ContainerRow[]>([NEW_CONTAINER(1), NEW_CONTAINER(2)]);
  const [catalog, setCatalog] = useState<CatalogEntry[] | null>(null);
  const [catalogError, setCatalogError] = useState<Error | null>(null);
  const [validation, setValidation] = useState<ChainValidation | null>(null);
  const [submitError, setSubmitError] = useState<Error | null>(null);
  const creating = useOperation();

  const target: Target | undefined = useMemo(
    () => (targets.data ?? []).find((t) => `${t.sourceId}|${t.location}` === targetKey),
    [targets.data, targetKey],
  );
  const kind = target?.kind;

  useEffect(() => {
    setCatalog(null);
    setCatalogError(null);
    if (target?.kind !== "process") return;
    api.catalog(target.sourceId, target.location).then(setCatalog, setCatalogError);
  }, [target?.sourceId, target?.location, target?.kind]);

  const request: ChainCreate | null = target
    ? {
        sourceId: target.sourceId, location: target.location, kind: target.kind, name: name.trim(), start: true,
        processApps: target.kind === "process" ? proc : [],
        containerApps: target.kind === "container"
          ? cont.map(({ envText, ...c }) => ({ ...c, env: parseEnv(envText) }))
          : [],
      }
    : null;
  const debounced = useDebounced(JSON.stringify(request), 400);

  useEffect(() => {
    const req = JSON.parse(debounced) as ChainCreate | null;
    if (!req || !req.name) {
      setValidation(null);
      return;
    }
    let alive = true;
    api.validate(req).then((v) => alive && setValidation(v), (e) => alive && setValidation({
      valid: false, errors: [(e as Error).message, ...((e as { errors?: string[] }).errors ?? [])],
      warnings: [], preview: null, assignedPorts: [],
    }));
    return () => {
      alive = false;
    };
  }, [debounced]); // validujeme jen ustálený stav formuláře

  const submit = async (start: boolean) => {
    if (!request) return;
    setSubmitError(null);
    try {
      creating.track(await api.create({ ...request, start }));
    } catch (e) {
      setSubmitError(e as Error);
    }
  };

  const grouped = useMemo(() => {
    const g: Record<string, Target[]> = { process: [], container: [] };
    (targets.data ?? []).forEach((t) => g[t.kind].push(t));
    return g;
  }, [targets.data]);

  const done = creating.op && creating.op.status !== "running";

  return (
    <>
      <nav className="crumbs" aria-label="Drobečková navigace">
        <a href="#/">Řetězce</a>
        <span aria-hidden="true">/</span>
        <span>Nový řetězec</span>
      </nav>
      <div className="page-head">
        <div>
          <h1>Nový řetězec</h1>
          <p className="muted">Řetězec procesů běží na VM s agentem, řetězec kontejnerů jako stack v Komodu.</p>
        </div>
      </div>

      {creating.op && (
        <>
          <OperationPanel op={creating.op} onClose={creating.clear} />
          {done && creating.op.status === "succeeded" && (
            <p><a className="btn primary" href={`#/?q=${encodeURIComponent(name.trim())}`}>Zobrazit řetězec</a></p>
          )}
        </>
      )}

      <div className="cols">
        <div className="col-main">
          <section className="card">
            <h2>1. Kam a jak se jmenuje</h2>
            <div className="form-grid">
              <label>
                <span>Umístění</span>
                <select value={targetKey} onChange={(e) => setTargetKey(e.target.value)}>
                  <option value="">Vyberte VM nebo server…</option>
                  {(["process", "container"] as const).map((k) =>
                    grouped[k].length ? (
                      <optgroup key={k} label={k === "process" ? "VM s agentem (procesy)" : "Servery v Komodu (kontejnery)"}>
                        {grouped[k].map((t) => (
                          <option key={`${t.sourceId}|${t.location}`} value={`${t.sourceId}|${t.location}`} disabled={!t.reachable}>
                            {t.location}{t.environment ? ` · ${t.environment}` : ""}{t.reachable ? "" : " (nedostupné)"}
                          </option>
                        ))}
                      </optgroup>
                    ) : null,
                  )}
                </select>
              </label>
              <label>
                <span>Název řetězce</span>
                <input value={name} onChange={(e) => setName(e.target.value)} placeholder="např. Export pro partnery" maxLength={80} />
              </label>
            </div>
          </section>

          {!target && <Empty>Nejdřív vyberte, kde má řetězec běžet.</Empty>}

          {kind === "process" && (
            <section className="card">
              <h2>2. Aplikace v pořadí toku dat</h2>
              <ErrorBox error={catalogError} />
              {proc.length === 0 && <Empty>Přidejte aplikace z katalogu VM. První bude vstup řetězce.</Empty>}
              <ol className="rows">
                {proc.map((a, i) => {
                  const versions = catalog?.find((c) => c.type === a.type)?.versions ?? [a.version];
                  return (
                    <li key={`${a.type}-${i}`} className="row-item">
                      <span className="num">{i + 1}</span>
                      <strong className="grow">{a.type}</strong>
                      <label className="inline">
                        <span>Verze</span>
                        <select value={a.version} onChange={(e) => setProc(proc.map((x, j) => (j === i ? { ...x, version: e.target.value } : x)))}>
                          {versions.map((v) => <option key={v}>{v}</option>)}
                        </select>
                      </label>
                      <RowButtons i={i} n={proc.length} onMove={(d) => setProc(move(proc, i, d))}
                        onRemove={() => setProc(proc.filter((_, j) => j !== i))} label={a.type} />
                    </li>
                  );
                })}
              </ol>
              <h3>Katalog</h3>
              <div className="chips">
                {(catalog ?? []).map((c) => (
                  <button key={c.type} className="btn small" disabled={proc.some((p) => p.type === c.type)}
                    onClick={() => setProc([...proc, { type: c.type, version: c.versions[0] }])}>
                    <Icon name="plus" size={14} /> {c.type} <span className="mono muted">{c.versions[0]}</span>
                  </button>
                ))}
                {catalog === null && !catalogError && <span className="muted">Načítám katalog…</span>}
              </div>
            </section>
          )}

          {kind === "container" && (
            <section className="card">
              <h2>2. Kontejnery v pořadí toku dat</h2>
              <p className="muted small">
                Každý kontejner musí poskytovat povinné API aplikace. Sousedé se adresují jménem služby,
                CMU doplní proměnné CHAIN_* a APP_*.
              </p>
              <ol className="rows">
                {cont.map((c, i) => (
                  <li key={i} className="row-item container-row">
                    <span className="num">{i + 1}</span>
                    <div className="container-fields">
                      <label><span>Služba</span>
                        <input value={c.name} onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
                      </label>
                      <label className="wide"><span>Image</span>
                        <input value={c.image} placeholder="registry.local/app:1.0.0"
                          onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, image: e.target.value } : x)))} />
                      </label>
                      <label><span>Port API</span>
                        <input type="number" value={c.port} min={1} max={65535}
                          onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, port: Number(e.target.value) } : x)))} />
                      </label>
                      <label><span>Port na hostiteli</span>
                        <input type="number" value={c.publish ?? ""} placeholder="nepublikovat" min={1} max={65535}
                          onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, publish: e.target.value ? Number(e.target.value) : null } : x)))} />
                      </label>
                      <label className="wide"><span>Proměnné prostředí (KLÍČ=hodnota)</span>
                        <textarea rows={2} value={c.envText}
                          onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, envText: e.target.value } : x)))} />
                      </label>
                      <label className="check">
                        <input type="checkbox" checked={c.healthcheck}
                          onChange={(e) => setCont(cont.map((x, j) => (j === i ? { ...x, healthcheck: e.target.checked } : x)))} />
                        <span>Healthcheck /health/ready</span>
                      </label>
                    </div>
                    <RowButtons i={i} n={cont.length} onMove={(d) => setCont(move(cont, i, d))}
                      onRemove={() => setCont(cont.filter((_, j) => j !== i))} label={c.name} />
                  </li>
                ))}
              </ol>
              <button className="btn small" onClick={() => setCont([...cont, NEW_CONTAINER(cont.length + 1)])}>
                <Icon name="plus" size={14} /> Přidat kontejner
              </button>
            </section>
          )}
        </div>

        <aside className="col-side">
          <section className="card sticky">
            <h2>3. Kontrola</h2>
            {!validation ? (
              <p className="muted small">Vyplňte umístění, název a aplikace.</p>
            ) : (
              <ul className="checks">
                {validation.errors.map((e) => <li key={e} className="s-problem">✕ {e}</li>)}
                {validation.warnings.map((w) => <li key={w} className="s-warning">! {w}</li>)}
                {validation.valid && <li className="s-ok">✓ Řetězec je v pořádku</li>}
                {validation.assignedPorts.length > 0 && kind === "process" && (
                  <li className="muted">Porty od agenta: <span className="mono">{validation.assignedPorts.join(", ")}</span></li>
                )}
              </ul>
            )}
            <ErrorBox error={submitError} />
            <div className="stack">
              <button className="btn primary" disabled={!validation?.valid || creating.op?.status === "running"} onClick={() => void submit(true)}>
                Vytvořit a spustit
              </button>
              <button className="btn" disabled={!validation?.valid || creating.op?.status === "running"} onClick={() => void submit(false)}>
                Uložit bez spuštění
              </button>
            </div>
            {validation?.preview && (
              <details className="preview" open>
                <summary>docker-compose.yaml pro Komodo</summary>
                <pre>{validation.preview}</pre>
              </details>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}

function RowButtons({ i, n, onMove, onRemove, label }: {
  i: number; n: number; onMove: (d: number) => void; onRemove: () => void; label: string;
}) {
  return (
    <span className="row-buttons">
      <button className="icon-btn" disabled={i === 0} onClick={() => onMove(-1)} aria-label={`Posunout ${label} nahoru`}>
        <Icon name="up" />
      </button>
      <button className="icon-btn" disabled={i === n - 1} onClick={() => onMove(1)} aria-label={`Posunout ${label} dolů`}>
        <Icon name="down" />
      </button>
      <button className="icon-btn danger" onClick={onRemove} aria-label={`Odebrat ${label}`}>
        <Icon name="x" />
      </button>
    </span>
  );
}
