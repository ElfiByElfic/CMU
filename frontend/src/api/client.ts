import type {
  CatalogEntry,
  Chain,
  ChainCreate,
  ChainValidation,
  ComponentStatus,
  Location,
  Me,
  Operation,
  Summary,
  Target,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "cmu.token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public errors: string[] = [],
  ) {
    super(message);
  }
}

type AuthListener = () => void;
const authListeners = new Set<AuthListener>();

export function onAuthRequired(fn: AuthListener): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}

export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* prohlížeč bez úložiště – token platí jen do obnovení stránky */
  }
  memoryToken = token;
}

let memoryToken = "";

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = getToken() || memoryToken;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(BASE + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "Backend je nedostupný");
  }
  if (res.status === 401) authListeners.forEach((fn) => fn());
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const p = (data ?? {}) as { title?: string; detail?: string; errors?: string[] };
    throw new ApiError(res.status, p.detail || p.title || `HTTP ${res.status}`, p.errors ?? []);
  }
  return data as T;
}

const enc = encodeURIComponent;

export const api = {
  me: () => request<Me>("GET", "/me"),
  summary: () => request<Summary>("GET", "/summary"),
  chains: () => request<Chain[]>("GET", "/chains"),
  chain: (id: string) => request<Chain>("GET", `/chains/${enc(id)}`),
  chainAction: (id: string, action: string) => request<Operation>("POST", `/chains/${enc(id)}/actions/${action}`),
  deleteChain: (id: string) => request<Operation>("DELETE", `/chains/${enc(id)}`),
  appAction: (id: string, appId: string, action: string) =>
    request<Operation>("POST", `/chains/${enc(id)}/apps/${enc(appId)}/actions/${action}`),
  appLogs: (id: string, appId: string, tail = 200) =>
    request<{ lines: string[] }>("GET", `/chains/${enc(id)}/apps/${enc(appId)}/logs?tail=${tail}`),
  operation: (id: string) => request<Operation>("GET", `/operations/${enc(id)}`),
  locations: () => request<Location[]>("GET", "/locations"),
  components: () => request<ComponentStatus[]>("GET", "/components"),
  targets: () => request<Target[]>("GET", "/targets"),
  catalog: (sourceId: string, location: string) =>
    request<CatalogEntry[]>("GET", `/targets/${enc(sourceId)}/${enc(location)}/catalog`),
  validate: (req: ChainCreate) => request<ChainValidation>("POST", "/chains/validate", req),
  create: (req: ChainCreate) => request<Operation>("POST", "/chains", req),
};
