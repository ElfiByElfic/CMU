import { useMemo } from "react";
import { api } from "../api/client";
import type { Chain, Health } from "../api/types";
import { ChainTile } from "../components/ChainTile";
import { MultiSelect, type Option } from "../components/MultiSelect";
import { Empty, ErrorBox, Icon } from "../components/ui";
import {
  type Dim, FLAGS, type Flag, Index, NO_PROBLEM, PROBLEM, isFiltered, readFilters, splitVersionKey, versionKey,
} from "../lib/filters";
import { HEALTH, HEALTH_ORDER, KIND, ago } from "../lib/format";
import { navigate, updateParams, useNow, usePoll } from "../lib/hooks";

type Sort = "health" | "name" | "host";

const DIM_LABEL: Record<Dim, string> = {
  health: "Stav", host: "Hostitel", chain: "Řetězec", version: "Verze", kind: "Typ", env: "Prostředí", flag: "Další",
};

export function Dashboard({ params, canCreate }: { params: URLSearchParams; canCreate: boolean }) {
  const chains = usePoll(api.chains, 5000);
  const locations = usePoll(api.locations, 15000);
  const now = useNow();

  const f = readFilters(params);
  const sort = (params.get("sort") || "health") as Sort;
  const set = (d: Dim, values: string[]) => updateParams({ [d]: values, ...(d === "host" ? { location: undefined } : {}) });

  const all = chains.data ?? [];
  const index = useMemo(() => new Index(all), [all]);
  const key = params.toString();

  const visible = useMemo(() => {
    const list = all.filter((c) => index.matches(c, f));
    const byName = (a: Chain, b: Chain) => a.name.localeCompare(b.name, "cs") || a.location.name.localeCompare(b.location.name);
    if (sort === "name") list.sort(byName);
    if (sort === "host") list.sort((a, b) => a.location.name.localeCompare(b.location.name) || a.name.localeCompare(b.name, "cs"));
    return list;
  }, [all, index, key]); // f a sort jsou odvozené z key

  const facets = useMemo(() => {
    const out = {} as Record<Dim, Map<string, number>>;
    (["health", "host", "chain", "version", "kind", "env", "flag"] as Dim[]).forEach((d) => (out[d] = index.facet(all, f, d)));
    return out;
  }, [all, index, key]);

  const options = useMemo(() => buildOptions(all, facets), [all, facets]);
  const unreachable = (locations.data ?? []).filter((l) => !l.reachable);
  const filtered = isFiltered(f);
  const hostCount = new Set(all.map((c) => c.location.name)).size;

  const healthSet = new Set(f.health);
  const preset = sameSet(f.health, PROBLEM) ? "problem" : sameSet(f.health, NO_PROBLEM) ? "ok" : f.health.length ? "custom" : "all";

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Řetězce</h1>
          <p className="muted">
            {all.length} řetězců na {hostCount} hostitelích
            {chains.updatedAt && <> · obnoveno {ago(new Date(chains.updatedAt).toISOString(), now)}</>}
          </p>
        </div>
        {canCreate && (
          <a className="btn primary" href="#/new">
            <Icon name="plus" /> Nový řetězec
          </a>
        )}
      </div>

      <div className="status-row">
        <div className="segmented" role="group" aria-label="Rychlý filtr">
          {([["all", "Vše", []], ["problem", "S problémem", PROBLEM], ["ok", "Bez problému", NO_PROBLEM]] as const).map(([k, l, v]) => (
            <button key={k} className={preset === k ? "active" : ""} aria-pressed={preset === k} onClick={() => set("health", [...v])}>
              {l}
            </button>
          ))}
        </div>
        <div className="summary" role="group" aria-label="Filtr podle stavu">
          {HEALTH_ORDER.map((h) => (
            <button key={h} className={`summary-chip ${HEALTH[h].cls} ${healthSet.has(h) ? "active" : ""}`}
              aria-pressed={healthSet.has(h)}
              onClick={() => set("health", healthSet.has(h) ? f.health.filter((x) => x !== h) : [...f.health, h])}>
              <span className="dot" aria-hidden="true" />
              <span className="n">{facets.health.get(h) ?? 0}</span>
              <span>{HEALTH[h].label}</span>
            </button>
          ))}
        </div>
      </div>

      {unreachable.length > 0 && (
        <a className="banner" href="#/sources">
          <Icon name="alert" />
          {unreachable.length === 1 ? "Nedostupný hostitel: " : `Nedostupní hostitelé (${unreachable.length}): `}
          <span className="mono">{unreachable.slice(0, 5).map((l) => l.name).join(", ")}{unreachable.length > 5 ? " …" : ""}</span>
          <span className="muted">– jejich řetězce ukazujeme bez stavu</span>
        </a>
      )}

      <div className="filters">
        <label className="search">
          <Icon name="search" />
          <span className="sr-only">Hledat</span>
          <input type="search" placeholder="Hledat v názvech, hostitelích, aplikacích, image, výstrahách…" value={f.q}
            onChange={(e) => updateParams({ q: e.target.value })} />
        </label>
        <MultiSelect label="Hostitel" options={options.host} selected={f.host} onChange={(v) => set("host", v)} searchable />
        <MultiSelect label="Řetězec" options={options.chain} selected={f.chain} onChange={(v) => set("chain", v)} searchable />
        <MultiSelect label="Verze" options={options.version} selected={f.version} onChange={(v) => set("version", v)} searchable />
        <MultiSelect label="Typ" options={options.kind} selected={f.kind} onChange={(v) => set("kind", v)} searchable={false} />
        <MultiSelect label="Prostředí" options={options.env} selected={f.env} onChange={(v) => set("env", v)} searchable={false} />
        <MultiSelect label="Další" options={options.flag} selected={f.flag} onChange={(v) => set("flag", v)} searchable={false} />
        <label className="select">
          <span className="sr-only">Řazení</span>
          <select value={sort} aria-label="Řazení" onChange={(e) => updateParams({ sort: e.target.value === "health" ? undefined : e.target.value })}>
            <option value="health">Nejdřív problémy</option>
            <option value="name">Podle názvu řetězce</option>
            <option value="host">Podle hostitele</option>
          </select>
        </label>
      </div>

      {filtered && (
        <div className="active-filters" aria-label="Použité filtry">
          {(["health", "host", "chain", "version", "kind", "env", "flag"] as Dim[]).flatMap((d) =>
            f[d].map((v) => (
              <button key={`${d}:${v}`} className="filter-chip" onClick={() => set(d, f[d].filter((x) => x !== v))}
                aria-label={`Odebrat filtr ${DIM_LABEL[d]}: ${chipLabel(d, v)}`}>
                <span className="muted">{DIM_LABEL[d]}:</span> {chipLabel(d, v)} <Icon name="x" size={12} />
              </button>
            )),
          )}
          {f.q.trim() && (
            <button className="filter-chip" onClick={() => updateParams({ q: undefined })} aria-label="Odebrat hledání">
              <span className="muted">Hledání:</span> „{f.q.trim()}“ <Icon name="x" size={12} />
            </button>
          )}
          <button className="btn ghost small" onClick={() => navigate("/", sort === "health" ? {} : { sort })}>
            Zrušit filtry
          </button>
          <span className="muted small">Zobrazeno {visible.length} z {all.length}</span>
        </div>
      )}

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
    </>
  );
}

function sameSet(a: string[], b: readonly string[]): boolean {
  return a.length === b.length && b.every((x) => a.includes(x));
}

function chipLabel(d: Dim, v: string): string {
  if (d === "health") return HEALTH[v as Health]?.label ?? v;
  if (d === "kind") return KIND[v as keyof typeof KIND] ?? v;
  if (d === "flag") return FLAGS[v as Flag]?.label ?? v;
  if (d === "version") return splitVersionKey(v).join(" ");
  return v;
}

function versionCmp(a: string, b: string): number {
  const pa = a.split(/[.-]/), pb = b.split(/[.-]/);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? "", y = pb[i] ?? "";
    const nx = Number(x), ny = Number(y);
    const c = !Number.isNaN(nx) && !Number.isNaN(ny) && x !== "" && y !== "" ? nx - ny : x.localeCompare(y);
    if (c) return c;
  }
  return 0;
}

function buildOptions(all: Chain[], facets: Record<Dim, Map<string, number>>): Record<Dim, Option[]> {
  const count = (d: Dim, v: string) => facets[d].get(v) ?? 0;

  const hosts = new Map<string, Chain["location"]>();
  all.forEach((c) => hosts.set(c.location.name, c.location));
  const host = [...hosts.values()]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map((l) => ({
      value: l.name, label: l.name, count: count("host", l.name),
      hint: [l.address, l.environment].filter(Boolean).join(" · "),
      group: l.kind === "docker-host" ? "Docker servery (Komodo)" : "VM s agentem",
    }));

  const chainHosts = new Map<string, Set<string>>();
  all.forEach((c) => {
    if (!chainHosts.has(c.name)) chainHosts.set(c.name, new Set());
    chainHosts.get(c.name)!.add(c.location.name);
  });
  const chain = [...chainHosts.entries()]
    .sort(([a], [b]) => a.localeCompare(b, "cs"))
    .map(([name, hs]) => ({ value: name, label: name, count: count("chain", name), hint: `na ${hs.size} ${hs.size === 1 ? "hostiteli" : "hostitelích"}` }));

  const versionKeys = new Set<string>();
  all.forEach((c) => c.apps.forEach((a) => versionKeys.add(versionKey(a))));
  const version = [...versionKeys]
    .map((k) => splitVersionKey(k))
    .sort(([an, av], [bn, bv]) => an.localeCompare(bn) || versionCmp(bv, av))
    .map(([name, ver]) => ({ value: `${name}@${ver}`, label: ver, group: name, count: count("version", `${name}@${ver}`) }));

  const kind = (["process", "container"] as const).map((k) => ({ value: k, label: KIND[k], count: count("kind", k) }));
  const envs = [...new Set(all.map((c) => c.location.environment).filter(Boolean) as string[])].sort();
  const env = envs.map((e) => ({ value: e, label: e, count: count("env", e) }));
  const flag = (Object.keys(FLAGS) as Flag[]).map((k) => ({ value: k, label: FLAGS[k].label, hint: FLAGS[k].hint, count: count("flag", k) }));
  return { health: [], host, chain, version, kind, env, flag };
}
