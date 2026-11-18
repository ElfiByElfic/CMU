import { type FormEvent, useState } from "react";
import { api } from "../api/client";
import type { SavedView } from "../api/types";
import { normalizeQuery } from "../lib/filters";
import { usePoll } from "../lib/hooks";
import { ErrorBox, Icon } from "./ui";

type Mode = { kind: "idle" } | { kind: "save" } | { kind: "rename"; view: SavedView } | { kind: "delete"; view: SavedView };

/** Uložené pohledy: pojmenované kombinace filtrů, řazení a seskupení – sdílené pro celý tým. */
export function SavedViews({ query, canEdit, onOpen }: {
  query: string; canEdit: boolean; onOpen: (query: string) => void;
}) {
  const views = usePoll(api.views, 30000);
  const [mode, setMode] = useState<Mode>({ kind: "idle" });
  const [name, setName] = useState("");
  const [lastOpened, setLastOpened] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);

  const current = normalizeQuery(query);
  const list = views.data ?? [];
  const active = list.find((v) => normalizeQuery(v.query) === current) ?? null;
  const opened = list.find((v) => v.id === lastOpened) ?? null;
  const modified = opened && !active && current !== "" ? opened : null;

  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      setMode({ kind: "idle" });
      await views.reload();
    } catch (e) {
      setError(e as Error);
    }
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mode.kind === "save") {
      void run(async () => setLastOpened((await api.createView(name.trim(), current)).id));
    } else if (mode.kind === "rename") {
      void run(() => api.updateView(mode.view.id, name.trim(), mode.view.query));
    }
  };

  return (
    <section className="views" aria-label="Uložené pohledy">
      <div className="views-row">
        <span className="views-label">Pohledy</span>
        <a href="#/" className={`view-chip ${current === "" ? "active" : ""}`} aria-current={current === "" ? "page" : undefined}
          onClick={() => setLastOpened(null)}>
          Všechny řetězce
        </a>
        {list.map((v) => (
          <button key={v.id} className={`view-chip ${active?.id === v.id ? "active" : ""}`}
            aria-pressed={active?.id === v.id} title={v.query || "bez filtrů"}
            onClick={() => { setLastOpened(v.id); onOpen(v.query); }}>
            {v.name}
          </button>
        ))}
        {canEdit && mode.kind === "idle" && (
          <span className="views-actions">
            {modified && (
              <button className="btn small" onClick={() => void run(() => api.updateView(modified.id, modified.name, current))}>
                Uložit změny do „{modified.name}“
              </button>
            )}
            {!active && current !== "" && (
              <button className="btn small" onClick={() => { setName(""); setMode({ kind: "save" }); }}>
                <Icon name="plus" size={14} /> Uložit jako pohled
              </button>
            )}
            {active && (
              <>
                <button className="btn ghost small" onClick={() => { setName(active.name); setMode({ kind: "rename", view: active }); }}>
                  Přejmenovat
                </button>
                <button className="btn ghost small danger-text" onClick={() => setMode({ kind: "delete", view: active })}>
                  Smazat
                </button>
              </>
            )}
          </span>
        )}
      </div>

      {(mode.kind === "save" || mode.kind === "rename") && (
        <form className="views-form" onSubmit={submit}>
          <label>
            <span>{mode.kind === "save" ? "Název nového pohledu (uvidí ho celý tým)" : "Nový název pohledu"}</span>
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} autoFocus
              placeholder="např. Produkce – problémy" />
          </label>
          <button className="btn primary small" type="submit" disabled={!name.trim()}>Uložit</button>
          <button className="btn ghost small" type="button" onClick={() => setMode({ kind: "idle" })}>Zrušit</button>
        </form>
      )}
      {mode.kind === "delete" && (
        <div className="confirm small" role="alertdialog" aria-label="Smazání pohledu">
          <span>Smazat pohled „{mode.view.name}“ pro celý tým?</span>
          <span className="actions">
            <button className="btn small" onClick={() => setMode({ kind: "idle" })}>Zrušit</button>
            <button className="btn danger small" onClick={() => void run(() => api.deleteView(mode.view.id))}>Smazat</button>
          </span>
        </div>
      )}
      <ErrorBox error={error} />
    </section>
  );
}
