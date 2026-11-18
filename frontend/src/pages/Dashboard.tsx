import { useMemo, useState } from "react";
import { api } from "../api/client";
import type { Chain, Health, Severity } from "../api/types";
import { ChainTile } from "../components/ChainTile";
import { MultiSelect, type Option } from "../components/MultiSelect";
import { SavedViews } from "../components/SavedViews";
import { Empty, ErrorBox, HealthPill, Icon } from "../components/ui";
import {
  AGE, CHANGED, DIMS, type Dim, FLAGS, type Flag, GROUPS, type GroupBy, Index, NO_PROBLEM, PROBLEM, SEVERITY_ORDER,
  isFiltered, lastChangeOrObserved, readFilters, splitVersionKey, versionKey,
} from "../lib/filters";
import { HEALTH, HEALTH_ORDER, KIND, SEVERITY, ago } from "../lib/format";
import { navigate, navigateQuery, updateParams, useNow, usePoll } from "../lib/hooks";

type Sort = "health" | "name" | "host" | "changed";

const DIM_LABEL: Record<Dim, string> = {
  health: "Stav", host: "Hostitel", chain: "Řetězec", version: "Verze", severity: "Výstraha", kind: "Typ",
  env: "Prostředí", flag: "Podmínka",
};
const SECONDARY: Dim[] = ["kind", "env", "flag"];

export function Dashboard({ params, canCreate }: { params: URLSearchParams; canCreate: boolean }) {
  const chains = usePoll(api.chains, 5000);
  const locations = usePoll(api.locations, 15000);
  const now = useNow();

  const f = readFilters(params, now);
  const sort = (params.get("sort") || "health") as Sort;
  const group = ((params.get("group") || "") in GROUPS ? params.get("group") : "") as GroupBy;
  const set = (d: Dim, values: string[]) => updateParams({ [d]: values, ...(d === "host" ? { location: undefined } : {}) });

  const all = chains.data ?? [];
  const index = useMemo(() => new Index(all), [all]);
  const key = params.toString();
  const minute = Math.floor(now / 60000); // časové filtry stačí přepočítat jednou za minutu

  const visible = useMemo(() => {
    const list = all.filter((c) => index.matches(c, f));
    const byName = (a: Chain, b: Chain) => a.name.localeCompare(b.name, "cs") || a.location.name.localeCompare(b.location.name);
    if (sort === "name") list.sort(byName);
    if (sort === "host") list.sort((a, b) => a.location.name.localeCompare(b.location.name) || a.name.localeCompare(b.name, "cs"));
    if (sort === "changed") list.sort((a, b) => (lastChangeOrObserved(b) ?? 0) - (lastChangeOrObserved(a) ?? 0) || byName(a, b));
    return list;
  }, [all, index, key, minute]); // f a sort jsou odvozené z key

  const facets = useMemo(() => {
    const out = {} as Record<Dim, Map<string, number>>;
    DIMS.forEach((d) => (out[d] = index.facet(all, f, d)));
    return out;
  }, [all, index, key, minute]);

  const options = useMemo(() => buildOptions(all, facets), [all, facets]);
  const unreachable = (locations.data ?? []).filter((l) => !l.reachable);
  const filtered = isFiltered(f);
  const hostCount = new Set(all.map((c) => c.location.name)).size;
  const secondaryActive = SECONDARY.some((d) => f[d].length > 0) || Boolean(f.changed || f.age);
  const [moreOpen, setMoreOpen] = useState(secondaryActive);

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

      <SavedViews query={key} canEdit={canCreate} onOpen={(q) => navigateQuery("/", q)} />

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
        <MultiSelect label="Výstraha" options={options.severity} selected={f.severity} onChange={(v) => set("severity", v)}
          searchable={false} />
        <button className={`btn ghost more-toggle ${secondaryActive ? "has-active" : ""}`} aria-expanded={moreOpen}
          onClick={() => setMoreOpen(!moreOpen)}>
          Další filtry
          {secondaryActive && <span className="count">{SECONDARY.reduce((n, d) => n + f[d].length, 0) + (f.changed ? 1 : 0) + (f.age ? 1 : 0)}</span>}
          <Icon name={moreOpen ? "up" : "down"} size={14} />
        </button>
      </div>

      {(moreOpen || secondaryActive) && (
        <div className="filters secondary">
          <MultiSelect label="Typ" options={options.kind} selected={f.kind} onChange={(v) => set("kind", v)} searchable={false} />
          <MultiSelect label="Prostředí" options={options.env} selected={f.env} onChange={(v) => set("env", v)} searchable={false} />
          <MultiSelect label="Podmínky" options={options.flag} selected={f.flag} onChange={(v) => set("flag", v)} searchable={false} />
          <SingleSelect label="Poslední změna" value={f.changed} onChange={(v) => updateParams({ changed: v })}
            options={[["", "kdykoli"], ...Object.entries(CHANGED).map(([k, d]): [string, string] => [k, d.label])]} />
          <SingleSelect label="Stáří dat" value={f.age} onChange={(v) => updateParams({ age: v })}
            options={[["", "jakékoli"], ...Object.entries(AGE).map(([k, d]): [string, string] => [k, d.label])]} />
        </div>
      )}

      {filtered && (
        <div className="active-filters" aria-label="Použité filtry">
          {DIMS.flatMap((d) =>
            f[d].map((v) => (
              <button key={`${d}:${v}`} className="filter-chip" onClick={() => set(d, f[d].filter((x) => x !== v))}
                aria-label={`Odebrat filtr ${DIM_LABEL[d]}: ${chipLabel(d, v)}`}>
                <span className="muted">{DIM_LABEL[d]}:</span> {chipLabel(d, v)} <Icon name="x" size={12} />
              </button>
            )),
          )}
          {f.changed && (
            <button className="filter-chip" onClick={() => updateParams({ changed: undefined })} aria-label="Odebrat filtr Poslední změna">
              <span className="muted">Změna:</span> {CHANGED[f.changed].label} <Icon name="x" size={12} />
            </button>
          )}
          {f.age && (
            <button className="filter-chip" onClick={() => updateParams({ age: undefined })} aria-label="Odebrat filtr Stáří dat">
              <span className="muted">Data:</span> {AGE[f.age].label} <Icon name="x" size={12} />
            </button>
          )}
          {f.q.trim() && (
            <button className="filter-chip" onClick={() => updateParams({ q: undefined })} aria-label="Odebrat hledání">
              <span className="muted">Hledání:</span> „{f.q.trim()}“ <Icon name="x" size={12} />
            </button>
          )}
          <button className="btn ghost small" onClick={() => navigate("/", { sort: sort === "health" ? undefined : sort, group })}>
            Zrušit filtry
          </button>
        </div>
      )}

      <div className="display-row">
        <span className="muted small">{filtered ? `Zobrazeno ${visible.length} z ${all.length}` : `${all.length} řetězců`}</span>
        <span className="display-controls">
          <SingleSelect label="Seskupit podle" value={group} onChange={(v) => updateParams({ group: v })}
            options={[["", "neseskupovat"], ...Object.entries(GROUPS).map(([k, l]): [string, string] => [k, l])]} />
          <SingleSelect label="Řadit" value={sort} onChange={(v) => updateParams({ sort: v === "health" ? undefined : v })}
            options={[["health", "nejdřív problémy"], ["name", "podle názvu řetězce"], ["host", "podle hostitele"],
              ["changed", "podle poslední změny"]]} />
        </span>
      </div>

      <ErrorBox error={chains.error} />

      {chains.loading && !chains.data ? (
        <Empty>Načítám…</Empty>
      ) : visible.length === 0 ? (
        <Empty>{filtered ? "Filtru neodpovídá žádný řetězec." : "Zatím tu nejsou žádné řetězce."}</Empty>
      ) : group ? (
        <Groups chains={visible} group={group} sort={sort} now={now} />
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

// ------------------------------------------------------------------ seskupení

interface Group {
  key: string;
  title: string;
  meta: string;
  chains: Chain[];
  worst: number;
}

function groupOf(c: Chain, g: Exclude<GroupBy, "">): { key: string; title: string; meta: string } {
  switch (g) {
    case "host": {
      const l = c.location;
      return { key: l.name, title: l.name, meta: [l.kind === "docker-host" ? "Docker (Komodo)" : "VM", l.address, l.environment].filter(Boolean).join(" · ") };
    }
    case "chain": return { key: c.name, title: c.name, meta: "" };
    case "env": return { key: c.location.environment ?? "", title: c.location.environment ?? "bez prostředí", meta: "" };
    case "health": return { key: c.health, title: HEALTH[c.health].label, meta: "" };
    case "kind": return { key: c.kind, title: KIND[c.kind], meta: "" };
  }
}

function Groups({ chains, group, sort, now }: { chains: Chain[]; group: Exclude<GroupBy, "">; sort: Sort; now: number }) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const groups = useMemo(() => {
    const m = new Map<string, Group>();
    chains.forEach((c) => {
      const g = groupOf(c, group);
      if (!m.has(g.key)) m.set(g.key, { ...g, chains: [], worst: HEALTH_ORDER.length });
      const x = m.get(g.key)!;
      x.chains.push(c);
      x.worst = Math.min(x.worst, HEALTH_ORDER.indexOf(c.health));
    });
    const list = [...m.values()];
    list.forEach((x) => {
      if (group === "chain") {
        const hosts = new Set(x.chains.map((c) => c.location.name)).size;
        x.meta = `na ${hosts} ${hosts === 1 ? "hostiteli" : "hostitelích"}`;
      }
    });
    if (group === "health") list.sort((a, b) => a.worst - b.worst);
    else if (sort === "health") list.sort((a, b) => a.worst - b.worst || a.title.localeCompare(b.title, "cs"));
    else list.sort((a, b) => a.title.localeCompare(b.title, "cs"));
    return list;
  }, [chains, group, sort]);

  const toggle = (k: string) => {
    const next = new Set(collapsed);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setCollapsed(next);
  };

  return (
    <div className="groups">
      <div className="group-tools">
        <button className="linklike" onClick={() => setCollapsed(new Set(groups.map((g) => g.key)))}>Sbalit vše</button>
        <button className="linklike" onClick={() => setCollapsed(new Set())}>Rozbalit vše</button>
      </div>
      {groups.map((g) => {
        const open = !collapsed.has(g.key);
        const counts = HEALTH_ORDER.map((h) => [h, g.chains.filter((c) => c.health === h).length] as const).filter(([, n]) => n > 0);
        return (
          <section key={g.key} className="group" aria-label={`Skupina ${g.title}`}>
            <button className="group-head" aria-expanded={open} onClick={() => toggle(g.key)}>
              <Icon name={open ? "down" : "up"} size={16} />
              <span className="group-title">{g.title}</span>
              {g.meta && <span className="muted small">{g.meta}</span>}
              <span className="group-counts">
                {counts.map(([h, n]) => <HealthPill key={h} health={h} label={`${n} ${HEALTH[h].label.toLowerCase()}`} />)}
              </span>
              <span className="muted small group-n">{g.chains.length}</span>
            </button>
            {open && (
              <div className="grid">
                {g.chains.map((c) => <ChainTile key={c.id} chain={c} now={now} />)}
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

// ------------------------------------------------------------------ pomocné

function sameSet(a: string[], b: readonly string[]): boolean {
  return a.length === b.length && b.every((x) => a.includes(x));
}

function chipLabel(d: Dim, v: string): string {
  if (d === "health") return HEALTH[v as Health]?.label ?? v;
  if (d === "kind") return KIND[v as keyof typeof KIND] ?? v;
  if (d === "flag") return FLAGS[v as Flag]?.label ?? v;
  if (d === "severity") return v === "none" ? "bez výstrahy" : SEVERITY[v as Severity]?.label ?? v;
  if (d === "version") return splitVersionKey(v).join(" ");
  return v;
}

function SingleSelect({ label, value, onChange, options }: {
  label: string; value: string; onChange: (v: string) => void; options: [string, string][];
}) {
  return (
    <label className={`single ${value ? "single-active" : ""}`}>
      <span className="single-label">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
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

  const severity = SEVERITY_ORDER.map((s) => ({
    value: s, label: s === "none" ? "Bez výstrahy" : SEVERITY[s].label, count: count("severity", s),
    hint: s === "disaster" || s === "high" ? "řetězec je kvůli ní Problém" : s === "average" || s === "warning" ? "řetězec je kvůli ní Varování" : undefined,
  }));
  const kind = (["process", "container"] as const).map((k) => ({ value: k, label: KIND[k], count: count("kind", k) }));
  const envs = [...new Set(all.map((c) => c.location.environment).filter(Boolean) as string[])].sort();
  const env = envs.map((e) => ({ value: e, label: e, count: count("env", e) }));
  const flag = (Object.keys(FLAGS) as Flag[]).map((k) => ({ value: k, label: FLAGS[k].label, hint: FLAGS[k].hint, count: count("flag", k) }));
  return { health: [], host, chain, version, severity, kind, env, flag };
}
