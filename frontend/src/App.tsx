import { type FormEvent, useEffect, useState } from "react";
import { api, onAuthRequired, setToken } from "./api/client";
import type { Me } from "./api/types";
import { type AuthConfig, authConfig, hasOidcSession, login, logout } from "./auth/oidc";
import { Icon } from "./components/ui";
import { useRoute } from "./lib/hooks";
import { ChainDetail } from "./pages/ChainDetail";
import { Dashboard } from "./pages/Dashboard";
import { NewChain } from "./pages/NewChain";
import { Sources } from "./pages/Sources";

const ROLE_LABEL: Record<Me["role"], string> = { operator: "Operátor", viewer: "Jen čtení", none: "Bez role" };

export function App() {
  const { path, params } = useRoute();
  const cfg = authConfig();
  const [me, setMe] = useState<Me | null>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const [session, setSession] = useState(0); // po přihlášení stránku znovu připojit → načte data s tokenem

  const loadMe = () => api.me().then(setMe, () => setMe(null));
  useEffect(() => {
    void loadMe();
    return onAuthRequired(() => setNeedLogin(true));
  }, []);
  useEffect(() => {
    // bez přihlášení; přihlášený uživatel bez role dostane vlastní obrazovku, ne znovu přihlášení
    if (me && me.authEnabled && me.role === "none" && !me.user) setNeedLogin(true);
  }, [me]);

  const signOut = () => {
    setToken("");
    setMe(null);
    if (hasOidcSession()) logout();
    else setNeedLogin(true);
  };

  const canOperate = me?.role === "operator";
  const chainMatch = path.match(/^\/chains\/(.+)$/);
  const noRole = me?.authEnabled && me.role === "none" && me.user;

  let page;
  if (noRole) page = <NoRole me={me} onLogout={signOut} />;
  else if (chainMatch) page = <ChainDetail key={chainMatch[1]} id={decodeURIComponent(chainMatch[1])} canOperate={canOperate} />;
  else if (path === "/new") page = <NewChain />;
  else if (path === "/sources") page = <Sources />;
  else page = <Dashboard params={params} canCreate={canOperate} />;

  const nav = [
    { href: "#/", label: "Řetězce", active: path === "/" || Boolean(chainMatch) },
    { href: "#/sources", label: "Zdroje a integrace", active: path === "/sources" },
  ];

  return (
    <>
      <header className="topbar">
        <a className="brand" href="#/">
          <Icon name="grid" size={22} />
          <span>CMU</span>
          <span className="muted brand-sub">Centrální mozek</span>
        </a>
        <nav className="topnav" aria-label="Hlavní navigace">
          {nav.map((n) => (
            <a key={n.href} href={n.href} className={n.active ? "active" : ""} aria-current={n.active ? "page" : undefined}>
              {n.label}
            </a>
          ))}
        </nav>
        <div className="topbar-right">
          {me && (
            <span className="role" title={me.groups.length ? `Skupiny: ${me.groups.join(", ")}` : undefined}>
              {me.authEnabled ? (
                <>
                  {me.user && <strong className="user">{me.user}</strong>}
                  {me.via === "token" && !me.user && <span>API token · </span>}
                  {ROLE_LABEL[me.role]}
                </>
              ) : "Bez přihlášení"}
            </span>
          )}
          {me?.authEnabled && (me.role !== "none" || me.user) && (
            <button className="btn ghost small" onClick={signOut}>Odhlásit</button>
          )}
        </div>
      </header>
      <main className="main" key={session}>{page}</main>
      {needLogin && (
        <Login cfg={cfg} onDone={() => { setNeedLogin(false); void loadMe(); setSession((n) => n + 1); }} />
      )}
    </>
  );
}

function NoRole({ me, onLogout }: { me: Me; onLogout: () => void }) {
  return (
    <div className="card narrow">
      <h1>Nemáte přístup</h1>
      <p>
        Uživatel <strong>{me.user}</strong> je přihlášený, ale nepatří do žádné skupiny, které CMU přiřazuje roli.
      </p>
      <p className="muted small">
        Požádejte správce o přidání do skupiny pro čtení nebo pro operátory (v LDAPu).
        {me.groups.length > 0 && <> Vaše skupiny: <span className="mono">{me.groups.join(", ")}</span>.</>}
      </p>
      <button className="btn" onClick={onLogout}>Odhlásit a přihlásit jako jiný uživatel</button>
    </div>
  );
}

function Login({ cfg, onDone }: { cfg: AuthConfig; onDone: () => void }) {
  const oidc = cfg.mode === "oidc";
  const [showToken, setShowToken] = useState(!oidc);
  const [value, setValue] = useState("");
  const [error, setError] = useState(cfg.error ?? "");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setToken(value.trim());
    try {
      const me = await api.me();
      if (me.role === "none") throw new Error("Neplatný token");
      onDone();
    } catch (err) {
      setToken("");
      setError((err as Error).message);
    }
  };

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-labelledby="login-title">
        <h2 id="login-title">Přihlášení</h2>
        {error && <p className="s-problem small" role="alert">{error}</p>}
        {oidc && (
          <>
            <p className="muted small">Přihlaste se firemním účtem. Role se přidělují podle skupin v LDAPu.</p>
            <button className="btn primary" disabled={busy || !cfg.authorizationEndpoint}
              onClick={() => { setBusy(true); void login(); }}>
              Přihlásit se přes Keycloak
            </button>
            {cfg.tokensAllowed && !showToken && (
              <button className="linklike small" onClick={() => setShowToken(true)}>Přihlásit API tokenem</button>
            )}
          </>
        )}
        {showToken && (
          <form onSubmit={submit} className="stack">
            {!oidc && <p className="muted small">Zadejte API token backendu (role viewer nebo operator).</p>}
            <label>
              <span>Token</span>
              <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus={!oidc} />
            </label>
            <button className={`btn ${oidc ? "" : "primary"}`} type="submit" disabled={!value.trim()}>Přihlásit tokenem</button>
          </form>
        )}
      </div>
    </div>
  );
}
