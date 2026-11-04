import type { ChainKind, ChainState, Health, Severity } from "../api/types";

export const HEALTH: Record<Health, { label: string; cls: string }> = {
  problem: { label: "Problém", cls: "h-problem" },
  warning: { label: "Varování", cls: "h-warning" },
  unknown: { label: "Nedostupné", cls: "h-unknown" },
  stopped: { label: "Zastaveno", cls: "h-stopped" },
  ok: { label: "V pořádku", cls: "h-ok" },
};

export const HEALTH_ORDER: Health[] = ["problem", "warning", "unknown", "stopped", "ok"];

export const CHAIN_STATE: Record<ChainState, string> = {
  running: "Běží",
  degraded: "Zpomalený",
  broken: "Přerušený",
  starting: "Spouští se",
  stopping: "Zastavuje se",
  stopped: "Zastaven",
  unknown: "Stav neznámý",
};

export const KIND: Record<ChainKind, string> = { process: "Procesy", container: "Kontejnery" };

const APP_STATE: Record<string, { label: string; cls: string }> = {
  running: { label: "Běží", cls: "s-ok" },
  degraded: { label: "Degradováno", cls: "s-warning" },
  paused: { label: "Pozastaveno", cls: "s-warning" },
  draining: { label: "Dobíhá", cls: "s-warning" },
  starting: { label: "Spouští se", cls: "s-info" },
  stopping: { label: "Zastavuje se", cls: "s-info" },
  stopped: { label: "Zastaveno", cls: "s-stopped" },
  error: { label: "Chyba", cls: "s-problem" },
  unreachable: { label: "Neodpovídá", cls: "s-problem" },
  unknown: { label: "Neznámý", cls: "s-unknown" },
};

export function appState(state: string): { label: string; cls: string } {
  return APP_STATE[state] ?? { label: state, cls: "s-unknown" };
}

export const SEVERITY: Record<Severity, { label: string; cls: string }> = {
  disaster: { label: "Katastrofa", cls: "s-problem" },
  high: { label: "Vysoká", cls: "s-problem" },
  average: { label: "Střední", cls: "s-warning" },
  warning: { label: "Varování", cls: "s-warning" },
  info: { label: "Info", cls: "s-info" },
};

export function ago(iso: string | null, now: number): string {
  if (!iso) return "nikdy";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 10) return "právě teď";
  if (s < 60) return `před ${s} s`;
  if (s < 3600) return `před ${Math.round(s / 60)} min`;
  if (s < 86400) return `před ${Math.round(s / 3600)} h`;
  return new Date(iso).toLocaleString("cs-CZ");
}

export function time(iso: string | null): string {
  return iso ? new Date(iso).toLocaleTimeString("cs-CZ", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
}

export function num(v: number | null, unit?: string | null): string {
  if (v === null || Number.isNaN(v)) return "–";
  const s = Math.abs(v) >= 100 ? Math.round(v).toLocaleString("cs-CZ") : v.toLocaleString("cs-CZ", { maximumFractionDigits: 1 });
  return unit ? `${s} ${unit}`.replace(" /", "/") : s;
}

export const ACTION_LABEL: Record<string, string> = {
  start: "Spustit",
  stop: "Zastavit",
  restart: "Restartovat",
  reload: "Načíst konfiguraci",
  delete: "Odstranit",
};
