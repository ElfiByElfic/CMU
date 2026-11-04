import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Operation } from "../api/types";

/** Periodicky načítá data; při chybě drží poslední úspěšná data a vrací chybu zvlášť. */
export function usePoll<T>(fetcher: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async () => {
    try {
      const d = await fetcherRef.current();
      setData(d);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    const tick = () => {
      if (alive && document.visibilityState !== "hidden") void load();
    };
    tick();
    const t = window.setInterval(tick, intervalMs);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [intervalMs, load, ...deps]);

  return { data, error, loading, updatedAt, reload: load };
}

/** Sleduje operaci až do dokončení. */
export function useOperation(onDone?: (op: Operation) => void) {
  const [op, setOp] = useState<Operation | null>(null);
  const doneRef = useRef(onDone);
  doneRef.current = onDone;

  useEffect(() => {
    if (!op || op.status !== "running") return;
    const t = window.setTimeout(async () => {
      try {
        const next = await api.operation(op.id);
        setOp(next);
        if (next.status !== "running") doneRef.current?.(next);
      } catch (e) {
        setOp({ ...op, status: "failed", message: (e as Error).message });
      }
    }, 700);
    return () => window.clearTimeout(t);
  }, [op]);

  const track = useCallback((o: Operation) => {
    setOp(o);
    if (o.status !== "running") doneRef.current?.(o);
  }, []);

  return { op, track, clear: () => setOp(null) };
}

export function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = window.setTimeout(() => setV(value), ms);
    return () => window.clearTimeout(t);
  }, [value, ms]);
  return v;
}

/** Hash router: #/cesta?param=hodnota */
export function useRoute(): { path: string; params: URLSearchParams } {
  const parse = () => {
    const raw = window.location.hash.replace(/^#/, "") || "/";
    const [path, query = ""] = raw.split("?");
    return { path: path || "/", params: new URLSearchParams(query) };
  };
  const [route, setRoute] = useState(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

export function navigate(path: string, params?: Record<string, string | undefined>): void {
  const q = new URLSearchParams();
  Object.entries(params ?? {}).forEach(([k, v]) => v && q.set(k, v));
  const qs = q.toString();
  window.location.hash = path + (qs ? `?${qs}` : "");
}

export function useNow(intervalMs = 5000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(t);
  }, [intervalMs]);
  return now;
}
