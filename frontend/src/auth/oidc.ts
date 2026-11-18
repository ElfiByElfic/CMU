// Přihlášení přes Keycloak: OpenID Connect, Authorization Code flow s PKCE (veřejný klient, bez tajemství).
// Adresy Keycloaku a klienta dodá backend (GET /api/v1/auth/config), takže frontend nemá nic napevno.
// Tokeny jsou jen v sessionStorage daného panelu prohlížeče; obnovují se refresh tokenem před vypršením.

import { sha256 } from "./sha256";

export interface AuthConfig {
  mode: "none" | "token" | "oidc";
  issuer?: string;
  clientId?: string;
  scopes?: string;
  tokensAllowed?: boolean;
  authorizationEndpoint?: string;
  tokenEndpoint?: string;
  endSessionEndpoint?: string | null;
  error?: string;
}

interface Tokens {
  access: string;
  refresh: string | null;
  idToken: string | null;
  expiresAt: number; // ms
}

const TOKENS = "cmu.oidc.tokens";
const PENDING = "cmu.oidc.pending";

let config: AuthConfig = { mode: "none" };
let refreshing: Promise<string | null> | null = null;

const store = {
  get<T>(k: string): T | null {
    try {
      const v = sessionStorage.getItem(k);
      return v ? (JSON.parse(v) as T) : null;
    } catch {
      return null;
    }
  },
  set(k: string, v: unknown): void {
    try {
      if (v === null) sessionStorage.removeItem(k);
      else sessionStorage.setItem(k, JSON.stringify(v));
    } catch {
      /* úložiště nedostupné – přihlášení vydrží jen do obnovení stránky */
    }
  },
};
let memory: Tokens | null = null;

function b64url(bytes: Uint8Array): string {
  let s = "";
  bytes.forEach((b) => (s += String.fromCharCode(b)));
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function random(n = 32): string {
  const a = new Uint8Array(n);
  crypto.getRandomValues(a);
  return b64url(a);
}

async function challenge(verifier: string): Promise<string> {
  const data = new TextEncoder().encode(verifier);
  // crypto.subtle je jen v zabezpečeném kontextu (HTTPS nebo localhost); jinak výpočet v JS
  if (globalThis.crypto?.subtle) return b64url(new Uint8Array(await crypto.subtle.digest("SHA-256", data)));
  return b64url(sha256(data));
}

function redirectUri(): string {
  return window.location.origin + window.location.pathname;
}

function save(t: Tokens | null): void {
  memory = t;
  store.set(TOKENS, t);
}

function fromResponse(r: { access_token: string; refresh_token?: string; id_token?: string; expires_in?: number }, old?: Tokens | null): Tokens {
  return {
    access: r.access_token,
    refresh: r.refresh_token ?? old?.refresh ?? null,
    idToken: r.id_token ?? old?.idToken ?? null,
    expiresAt: Date.now() + (r.expires_in ?? 300) * 1000,
  };
}

async function tokenRequest(body: Record<string, string>): Promise<Tokens | null> {
  if (!config.tokenEndpoint) return null;
  const res = await fetch(config.tokenEndpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: config.clientId ?? "", ...body }),
  });
  if (!res.ok) return null;
  return fromResponse(await res.json(), memory);
}

/** Načte konfiguraci a dokončí návrat z Keycloaku (?code=…&state=…). Volat jednou při startu aplikace. */
export async function initAuth(): Promise<AuthConfig> {
  try {
    const res = await fetch("/api/v1/auth/config");
    if (res.ok) config = await res.json();
  } catch {
    config = { mode: "none", error: "Backend je nedostupný" };
  }
  memory = store.get<Tokens>(TOKENS);

  const q = new URLSearchParams(window.location.search);
  const code = q.get("code");
  if (config.mode === "oidc" && code) {
    const pending = store.get<{ verifier: string; state: string; hash: string }>(PENDING);
    store.set(PENDING, null);
    // adresu vyčistit, ať se kód neodešle znovu po obnovení stránky
    window.history.replaceState(null, "", redirectUri() + (pending?.hash ?? ""));
    if (pending && q.get("state") === pending.state) {
      const t = await tokenRequest({
        grant_type: "authorization_code", code, redirect_uri: redirectUri(), code_verifier: pending.verifier,
      });
      if (t) save(t);
      else config = { ...config, error: "Keycloak odmítl dokončit přihlášení" };
    } else {
      config = { ...config, error: "Neplatná odpověď přihlášení (state) – zkuste to znovu" };
    }
  } else if (q.get("error")) {
    config = { ...config, error: `Keycloak: ${q.get("error_description") ?? q.get("error")}` };
    window.history.replaceState(null, "", redirectUri() + window.location.hash);
  }
  return config;
}

export function authConfig(): AuthConfig {
  return config;
}

export function hasOidcSession(): boolean {
  return config.mode === "oidc" && memory !== null;
}

/** Přesměruje na přihlášení do Keycloaku; po návratu se aplikace vrátí na stejnou stránku. */
export async function login(): Promise<void> {
  if (!config.authorizationEndpoint) return;
  const verifier = random(48);
  const state = random(16);
  store.set(PENDING, { verifier, state, hash: window.location.hash });
  const params = new URLSearchParams({
    client_id: config.clientId ?? "", response_type: "code", redirect_uri: redirectUri(),
    scope: config.scopes ?? "openid", state, nonce: random(16),
    code_challenge: await challenge(verifier), code_challenge_method: "S256",
  });
  window.location.assign(`${config.authorizationEndpoint}?${params}`);
}

/** Platný přístupový token (případně obnovený), nebo null. */
export async function accessToken(): Promise<string | null> {
  if (config.mode !== "oidc" || !memory) return null;
  if (memory.expiresAt - Date.now() > 30_000) return memory.access;
  if (!memory.refresh) return memory.expiresAt > Date.now() ? memory.access : null;
  refreshing ??= (async () => {
    const t = await tokenRequest({ grant_type: "refresh_token", refresh_token: memory!.refresh! }).catch(() => null);
    save(t);
    refreshing = null;
    return t?.access ?? null;
  })();
  return refreshing;
}

/** Odhlášení: smaže tokeny a ukončí relaci v Keycloaku. */
export function logout(): void {
  const idToken = memory?.idToken;
  save(null);
  if (config.mode === "oidc" && config.endSessionEndpoint) {
    const params = new URLSearchParams({ client_id: config.clientId ?? "", post_logout_redirect_uri: redirectUri() });
    if (idToken) params.set("id_token_hint", idToken);
    window.location.assign(`${config.endSessionEndpoint}?${params}`);
  }
}

/** Zahodí neplatnou relaci (např. backend token odmítl). */
export function dropSession(): void {
  save(null);
}
