import { useMemo } from "react";
import { api } from "../api/client";
import type { Chain, ChainKind, Health } from "../api/types";
import { ChainTile } from "../components/ChainTile";
import { Empty, ErrorBox, Icon } from "../components/ui";
import { HEALTH, HEALTH_ORDER, KIND, ago } from "../lib/format";
import { navigate, useNow, usePoll } from "../lib/hooks";

type Sort = "health" | "name" | "location";

export function Dashboard({ params, canCreate }: { params: URLSearchParams; canCreate: boolean }) {
  const chains = usePoll(api.chains, 5000);
  const locations = usePoll(api.locations, 15000);
  const now = useNow();

  const health = (params.get("health") || "") as Health | "";
  const kind = (params.get("kind") || "") as ChainKind | "";
  const location = params.get("location") || "";
  const env = params.get("env") || "";
  const q = params.get("q") || "";
  const sort = (params.get("sort") || "health") as Sort;

  const set = (patch: Record<string, string>) => {
    const next: Record<string, string> = { health, kind, location, env, q, sort: sort === "health" ? "" : sort };
    navigate("/", { ...next, ...patch });
  };

  const all = chains.data ?? [];
  const counts = useMemo(() => {
    const c: Record<Health, number> = { problem: 0, warning: 0, unknown: 0, stopped: 0, ok: 0 };
    all.forEach((x) => (c[x.health] += 1));
    return c;
  }, [all]);

  const envs = useMemo(() => [...new Set(all.map((c) => c.location.environment).filter(Boolean))].sort() as string[], [all]);
  const locNames = useMemo(() => [...new Set(all.map((c) => c.location.name))].sort(), [all]);

  const visible = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const list = all.filter(
      (c) =>
        (!health || c.health === health) &&
        (!kind || c.kind === kind) &&
        (!location || c.location.name === location) &&
        (!env || c.location.environment === env) &&
        (!needle || haystack(c).includes(needle)),
    );
    if (sort === "name") list.sort((a, b) => a.name.localeCompare(b.name, "cs") || a.location.name.localeCompare(b.location.name));
    if (sort === "location") list.sort((a, b) => a.location.name.localeCompare(b.location.name) || a.name.localeCompare(b.name, "cs"));
    return list;
  }, [all, health, kind, location, env, q, sort]);

  const unreachable = (locations.data ?? []).filter((l) => !l.reachable);
  const filtered = Boolean(health || kind || location || env || q);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Řetězce</h1>
          <p className="muted">
            {all.length} řetězců na {locNames.length} lokalitách
            {chains.updatedAt && <> · obnoveno {ago(new Date(chains.updatedAt).toISOString(), now)}</>}
          </p>
        </div>
        {canCreate && (
          <a className="btn primary" href="#/new">
            <Icon name="plus" /> Nový řetězec
          </a>
        )}
      </div>

      <div className="summary" role="group" aria-label="Filtr podle stavu">
        {HEALTH_ORDER.map((h) => (
          <button key={h} className={`summary-chip ${HEALTH[h].cls} ${health === h ? "active" : ""}`}
            aria-pressed={health === h} onClick={() => set({ health: health === h ? "" : h })}>
            <span className="dot" aria-hidden="true" />
            <span className="n">{counts[h]}</span>
            <span>{HEALTH[h].label}</span>
          </button>
        ))}
      </div>

      {unreachable.length > 0 && (
        <a className="banner" href="#/sources">
          <Icon name="alert" />
          {unreachable.length === 1 ? "Nedostupná lokalita: " : `Nedostupné lokality (${unreachable.length}): `}
          <span className="mono">{unreachable.slice(0, 5).map((l) => l.name).join(", ")}{unreachable.length > 5 ? " …" : ""}</span>
          <span className="muted">– jejich řetězce ukazujeme bez stavu</span>
        </a>
      )}

      <div className="filters">
        <label className="search">
          <Icon name="search" />
          <span className="sr-only">Hledat</span>
          <input type="search" placeholder="Hledat řetězec, lokalitu, aplikaci, image…" value={q}
            onChange={(e) => set({ q: e.target.value })} />
        </label>
        <Select label="Typ" value={kind} onChange={(v) => set({ kind: v })}
          options={[["", "Všechny typy"], ["process", KIND.process], ["container", KIND.container]]} />
        <Select label="Lokalita" value={location} onChange={(v) => set({ location: v })}
          options={[["", "Všechny lokality"], ...locNames.map((n): [string, string] => [n, n])]} />
        <Select label="Prostředí" value={env} onChange={(v) => set({ env: v })}
          options={[["", "Všechna prostředí"], ...envs.map((n): [string, string] => [n, n])]} />
        <Select label="Řazení" value={sort} onChange={(v) => set({ sort: v === "health" ? "" : v })}
          options={[["health", "Nejdřív problémy"], ["name", "Podle názvu"], ["location", "Podle lokality"]]} />
        {filtered && (
          <button className="btn ghost" onClick={() => navigate("/")}>
            Zrušit filtry
          </button>
        )}
      </div>

      <ErrorBox error={chains.error} />

      {chains.loading && !chains.data ? (
        <Empty>Načítám…</Empty>
      ) : visible.length === 0 ? (
        <Empty>{filtered ? "Filtru neodpovídá žádný řetězec." : "Zatím tu nejsou žádné řetězce."}</Empty>
      ) : (
        <div className="grid" aria-live="polite">
          {visible.map((c) => (
            <ChainTile key={c.id} chain={c} now={now} />
          ))}
        </div>
      )}
      {filtered && visible.length > 0 && (
        <p className="muted small">
          Zobrazeno {visible.length} z {all.length}
        </p>
      )}
    </>
  );
}

function haystack(c: Chain): string {
  return [c.name, c.localId, c.location.name, c.location.address ?? "", ...c.apps.map((a) => `${a.name} ${a.image ?? ""} ${a.version ?? ""}`)]
    .join(" ")
    .toLowerCase();
}

function Select({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][];
}) {
  return (
    <label className="select">
      <span className="sr-only">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        {options.map(([v, l]) => (
          <option key={v} value={v}>{l}</option>
        ))}
      </select>
    </label>
  );
}
