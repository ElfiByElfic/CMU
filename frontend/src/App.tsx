import { type FormEvent, useEffect, useState } from "react";
import { api, onAuthRequired, setToken } from "./api/client";
import type { Me } from "./api/types";
import { Icon } from "./components/ui";
import { useRoute } from "./lib/hooks";
import { ChainDetail } from "./pages/ChainDetail";
import { Dashboard } from "./pages/Dashboard";
import { NewChain } from "./pages/NewChain";
import { Sources } from "./pages/Sources";

export function App() {
  const { path, params } = useRoute();
  const [me, setMe] = useState<Me | null>(null);
  const [needLogin, setNeedLogin] = useState(false);
  const [session, setSession] = useState(0); // po přihlášení stránku znovu připojit → načte data s tokenem

  const loadMe = () => api.me().then(setMe, () => setMe(null));
  useEffect(() => {
    void loadMe();
    return onAuthRequired(() => setNeedLogin(true));
  }, []);
  useEffect(() => {
    if (me?.role === "none" && me.authEnabled) setNeedLogin(true);
  }, [me]);

  const canOperate = me?.role === "operator";
  const chainMatch = path.match(/^\/chains\/(.+)$/);

  let page;
  if (chainMatch) page = <ChainDetail key={chainMatch[1]} id={decodeURIComponent(chainMatch[1])} canOperate={canOperate} />;
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
            <span className="role">
              {me.authEnabled ? (me.role === "operator" ? "Operátor" : me.role === "viewer" ? "Jen čtení" : "Nepřihlášen") : "Bez přihlášení"}
            </span>
          )}
          {me?.authEnabled && me.role !== "none" && (
            <button className="btn ghost small" onClick={() => { setToken(""); setNeedLogin(true); setMe(null); }}>
              Odhlásit
            </button>
          )}
        </div>
      </header>
      <main className="main" key={session}>{page}</main>
      {needLogin && (
        <Login onDone={() => { setNeedLogin(false); void loadMe(); setSession((n) => n + 1); }} />
      )}
    </>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
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
      <form className="modal" onSubmit={submit} aria-labelledby="login-title">
        <h2 id="login-title">Přihlášení</h2>
        <p className="muted small">Zadejte API token backendu (role viewer nebo operator).</p>
        <label>
          <span>Token</span>
          <input type="password" value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
        </label>
        {error && <p className="s-problem small">{error}</p>}
        <button className="btn primary" type="submit" disabled={!value.trim()}>Přihlásit</button>
      </form>
    </div>
  );
}
