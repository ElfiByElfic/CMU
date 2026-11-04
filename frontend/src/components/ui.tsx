import type { ReactNode } from "react";
import type { ChainApp, Health, Operation } from "../api/types";
import { ACTION_LABEL, HEALTH, appState } from "../lib/format";

export function HealthPill({ health, label }: { health: Health; label?: string }) {
  const h = HEALTH[health];
  return (
    <span className={`pill ${h.cls}`}>
      <span className="dot" aria-hidden="true" />
      {label ?? h.label}
    </span>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "accent" }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

export function Icon({ name, size = 16 }: { name: keyof typeof PATHS; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {PATHS[name]}
    </svg>
  );
}

const PATHS = {
  server: (
    <>
      <rect x="3" y="4" width="18" height="7" rx="1.5" />
      <rect x="3" y="13" width="18" height="7" rx="1.5" />
      <path d="M7 7.5h.01M7 16.5h.01" />
    </>
  ),
  box: (
    <>
      <path d="M21 8 12 3 3 8v8l9 5 9-5z" />
      <path d="M3 8l9 5 9-5M12 13v8" />
    </>
  ),
  plus: <path d="M12 5v14M5 12h14" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-3.5-3.5" />
    </>
  ),
  alert: (
    <>
      <path d="M12 3 2 20h20z" />
      <path d="M12 10v4M12 17h.01" />
    </>
  ),
  up: <path d="M6 15l6-6 6 6" />,
  down: <path d="M6 9l6 6 6-6" />,
  x: <path d="M6 6l12 12M18 6 6 18" />,
  grid: (
    <>
      <rect x="3" y="4" width="7" height="7" rx="1.5" />
      <rect x="14" y="4" width="7" height="7" rx="1.5" />
      <rect x="3" y="14" width="7" height="7" rx="1.5" />
      <rect x="14" y="14" width="7" height="7" rx="1.5" />
    </>
  ),
  refresh: (
    <>
      <path d="M20 11a8 8 0 1 0-2.3 5.7" />
      <path d="M20 4v7h-7" />
    </>
  ),
  link: (
    <>
      <path d="M14 4h6v6M20 4l-9 9" />
      <path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" />
    </>
  ),
};

/** Miniatura řetězce na dlaždici: čtverečky v pořadí toku dat. */
export function MiniFlow({ apps }: { apps: ChainApp[] }) {
  return (
    <div className="miniflow" role="list" aria-label="Aplikace v řetězci">
      {apps.map((a, i) => (
        <span key={a.id} className="miniflow-item" role="listitem">
          {i > 0 && <span className="miniflow-arrow" aria-hidden="true" />}
          <span className={`sq ${appState(a.state).cls}`} title={`${a.name}: ${appState(a.state).label}`} />
          <span className="sr-only">{`${a.name}: ${appState(a.state).label}`}</span>
        </span>
      ))}
    </div>
  );
}

export function OperationPanel({ op, onClose }: { op: Operation; onClose: () => void }) {
  const title = ACTION_LABEL[op.kind.split(".").pop() ?? ""] ?? op.kind;
  return (
    <div className={`op op-${op.status}`} role="status">
      <div className="op-head">
        <strong>{title}</strong>
        <span>{op.status === "running" ? "probíhá…" : op.status === "succeeded" ? "hotovo" : "selhalo"}</span>
        {op.status !== "running" && (
          <button className="icon-btn ghost" onClick={onClose} aria-label="Zavřít">
            <Icon name="x" />
          </button>
        )}
      </div>
      {op.steps.length > 0 && (
        <ol className="op-steps">
          {op.steps.map((s) => (
            <li key={s.target} className={`step-${s.status}`}>
              <span className="mono">{s.target}</span>
              <span>{s.message || s.status}</span>
            </li>
          ))}
        </ol>
      )}
      {op.message && op.status !== "running" && <p className="op-msg">{op.message}</p>}
    </div>
  );
}

export function ErrorBox({ error }: { error: Error | null }) {
  if (!error) return null;
  const errs = (error as Error & { errors?: string[] }).errors ?? [];
  return (
    <div className="error-box" role="alert">
      {error.message}
      {errs.length > 0 && (
        <ul>
          {errs.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
