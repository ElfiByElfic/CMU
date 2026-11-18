// Filtry dashboardu. Mezi různými filtry platí A ZÁROVEŇ, mezi hodnotami jednoho filtru NEBO.
// Stav filtrů žije v URL (?host=a&host=b&version=App2@2.4.0…), takže se dá sdílet odkazem.

import type { Chain, ChainApp, Health } from "../api/types";

export type Dim = "health" | "host" | "chain" | "version" | "kind" | "env" | "flag";
export const DIMS: Dim[] = ["health", "host", "chain", "version", "kind", "env", "flag"];

export type Flag = "alerts" | "stale" | "pending" | "prerelease" | "drift" | "restarts" | "busy";

export const FLAGS: Record<Flag, { label: string; hint: string }> = {
  alerts: { label: "Má výstrahu (Zabbix)", hint: "aktivní problém hostitele ve Zabbixu" },
  drift: { label: "Rozdílné verze", hint: "stejný řetězec má na jiném hostiteli jinou verzi některé aplikace" },
  prerelease: { label: "Předběžná verze", hint: "některá aplikace běží ve verzi -rc, -beta, -alpha…" },
  pending: { label: "Čeká na restart", hint: "změněná konfigurace se projeví až po reloadu nebo restartu" },
  restarts: { label: "Restartovala se", hint: "některá aplikace se od startu agenta restartovala" },
  stale: { label: "Nedostupný hostitel", hint: "data nejsou aktuální" },
  busy: { label: "Probíhá operace", hint: "start, stop, úprava…" },
};

export const PROBLEM: Health[] = ["problem", "warning", "unknown"];
export const NO_PROBLEM: Health[] = ["ok", "stopped"];

export type Filters = Record<Dim, string[]> & { q: string };

export function readFilters(params: URLSearchParams): Filters {
  const f = { q: params.get("q") ?? "" } as Filters;
  DIMS.forEach((d) => {
    // kompatibilita se staršími odkazy: ?location=… a ?health=a,b
    const raw = d === "host" ? [...params.getAll("host"), ...params.getAll("location")] : params.getAll(d);
    f[d] = [...new Set(raw.flatMap((v) => (d === "health" ? v.split(",") : [v])).filter(Boolean))];
  });
  return f;
}

export function isFiltered(f: Filters): boolean {
  return Boolean(f.q.trim()) || DIMS.some((d) => f[d].length > 0);
}

/** Verze aplikace: u procesů z agenta `version`, u kontejnerů tag image. */
export function appVersion(a: ChainApp): string {
  if (a.version) return a.version;
  const img = a.image ?? "";
  const at = img.indexOf("@");
  if (at >= 0) return img.slice(at + 1, at + 20);
  const tag = img.slice(img.lastIndexOf("/") + 1).split(":")[1];
  return tag ?? "latest";
}

export const versionKey = (a: ChainApp) => `${a.name}@${appVersion(a)}`;

export function splitVersionKey(key: string): [string, string] {
  const i = key.lastIndexOf("@");
  return i < 0 ? [key, ""] : [key.slice(0, i), key.slice(i + 1)];
}

const PRERELEASE = /-(rc|alpha|beta|pre|dev|snapshot|nightly)/i;

/** Pro každý řetězec spočítá odvozené vlastnosti, které nejsou v datech přímo (např. rozdílné verze). */
export class Index {
  private drift = new Set<string>();

  constructor(chains: Chain[]) {
    const versions = new Map<string, Set<string>>(); // "řetězec|aplikace" → verze
    chains.forEach((c) =>
      c.apps.forEach((a) => {
        const k = `${c.name}|${a.name}`;
        if (!versions.has(k)) versions.set(k, new Set());
        versions.get(k)!.add(appVersion(a));
      }),
    );
    chains.forEach((c) => {
      if (c.apps.some((a) => (versions.get(`${c.name}|${a.name}`)?.size ?? 0) > 1)) this.drift.add(c.id);
    });
  }

  flags(c: Chain): Set<Flag> {
    const f = new Set<Flag>();
    if (c.alerts.length) f.add("alerts");
    if (c.stale) f.add("stale");
    if (c.apps.some((a) => a.configPending)) f.add("pending");
    if (c.apps.some((a) => PRERELEASE.test(appVersion(a)))) f.add("prerelease");
    if (this.drift.has(c.id)) f.add("drift");
    if (c.apps.some((a) => (a.restarts ?? 0) > 0)) f.add("restarts");
    if (c.busyOperation) f.add("busy");
    return f;
  }

  /** Hodnoty řetězce v dané dimenzi (řetězec může mít víc verzí i víc příznaků). */
  values(c: Chain, d: Dim): string[] {
    switch (d) {
      case "health": return [c.health];
      case "host": return [c.location.name];
      case "chain": return [c.name];
      case "version": return c.apps.map(versionKey);
      case "kind": return [c.kind];
      case "env": return c.location.environment ? [c.location.environment] : [];
      case "flag": return [...this.flags(c)];
    }
  }

  matches(c: Chain, f: Filters, skip?: Dim): boolean {
    for (const d of DIMS) {
      if (d === skip || f[d].length === 0) continue;
      const vals = this.values(c, d);
      // příznaky se kombinují A ZÁROVEŇ (má výstrahu a zároveň rozdílné verze), ostatní NEBO
      const ok = d === "flag" ? f[d].every((x) => vals.includes(x)) : f[d].some((x) => vals.includes(x));
      if (!ok) return false;
    }
    const needle = f.q.trim().toLowerCase();
    return !needle || haystack(c).includes(needle);
  }

  /** Počty pro nabídku filtru: kolik řetězců by zbylo, kdyby se k ostatním filtrům přidala tato hodnota. */
  facet(chains: Chain[], f: Filters, d: Dim): Map<string, number> {
    const counts = new Map<string, number>();
    chains.forEach((c) => {
      if (!this.matches(c, f, d)) return;
      new Set(this.values(c, d)).forEach((v) => counts.set(v, (counts.get(v) ?? 0) + 1));
    });
    return counts;
  }
}

export function haystack(c: Chain): string {
  return [
    c.name, c.localId, c.location.name, c.location.address ?? "", c.location.environment ?? "",
    ...c.apps.map((a) => `${a.name} ${a.image ?? ""} ${a.version ?? ""}`),
    ...c.alerts.map((a) => a.name),
  ].join(" ").toLowerCase();
}
