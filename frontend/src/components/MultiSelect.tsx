import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Icon } from "./ui";

export interface Option {
  value: string;
  label: string;
  hint?: string;
  group?: string;
  count: number;
}

/** Rozbalovací výběr více hodnot s hledáním a počty. Pro stovky hostitelů i verzí. */
export function MultiSelect({ label, options, selected, onChange, searchable }: {
  label: string;
  options: Option[];
  selected: string[];
  onChange: (values: string[]) => void;
  searchable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const sel = useMemo(() => new Set(selected), [selected]);
  // vybrané hodnoty, které v datech zrovna nejsou, nesmí z nabídky zmizet (jinak by nešly odebrat)
  const all = useMemo((): Option[] => {
    const known = new Set(options.map((o) => o.value));
    return [...options, ...selected.filter((v) => !known.has(v)).map((v) => ({ value: v, label: v, count: 0 }))];
  }, [options, selected]);
  const needle = q.trim().toLowerCase();
  const shown = all.filter((o) => !needle || `${o.label} ${o.hint ?? ""} ${o.group ?? ""}`.toLowerCase().includes(needle));

  const toggle = (v: string) => onChange(sel.has(v) ? selected.filter((x) => x !== v) : [...selected, v]);
  const groups = [...new Set(shown.map((o) => o.group ?? ""))];

  const summary = selected.length === 0 ? "vše" : selected.length === 1 ? (all.find((o) => o.value === selected[0])?.label ?? selected[0]) : `${selected.length} vybráno`;

  return (
    <div className={`ms ${selected.length ? "ms-active" : ""}`} ref={ref}>
      <button type="button" className="ms-button" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        <span className="ms-label">{label}</span>
        <span className="ms-value ellipsis">{summary}</span>
        <Icon name="down" size={14} />
      </button>
      {open && (
        <div className="ms-pop" id={id} role="dialog" aria-label={`Filtr: ${label}`}>
          {(searchable ?? all.length > 8) && (
            <input className="ms-search" type="search" placeholder={`Hledat – ${label.toLowerCase()}`} value={q} autoFocus
              onChange={(e) => setQ(e.target.value)} aria-label={`Hledat v nabídce ${label}`} />
          )}
          <div className="ms-tools">
            <button type="button" className="linklike" disabled={shown.length === 0}
              onClick={() => onChange([...new Set([...selected, ...shown.map((o) => o.value)])])}>
              Vybrat {needle ? "nalezené" : "vše"}
            </button>
            <button type="button" className="linklike" disabled={selected.length === 0} onClick={() => onChange([])}>
              Zrušit výběr
            </button>
          </div>
          <div className="ms-list">
            {shown.length === 0 && <p className="muted small ms-empty">Nic nenalezeno.</p>}
            {groups.map((g) => (
              <div key={g || "-"} role="group" aria-label={g || label}>
                {g && <div className="ms-group">{g}</div>}
                {shown.filter((o) => (o.group ?? "") === g).map((o) => (
                  <label key={o.value} className={`ms-opt ${o.count === 0 && !sel.has(o.value) ? "ms-zero" : ""}`} title={o.hint}>
                    <input type="checkbox" checked={sel.has(o.value)} onChange={() => toggle(o.value)} />
                    <span className="ms-opt-label">
                      {o.label}
                      {o.hint && <span className="muted small block">{o.hint}</span>}
                    </span>
                    <span className="ms-count mono">{o.count}</span>
                  </label>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
