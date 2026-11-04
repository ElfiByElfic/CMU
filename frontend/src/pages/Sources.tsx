import { Fragment } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Icon } from "../components/ui";
import { ago } from "../lib/format";
import { useNow, usePoll } from "../lib/hooks";

const TYPE_LABEL: Record<string, string> = {
  agent: "Agenti na VM",
  komodo: "Komodo",
  demo: "Demo data",
  zabbix: "Zabbix",
  prometheus: "Prometheus",
};

export function Sources() {
  const comps = usePoll(api.components, 5000);
  const locs = usePoll(api.locations, 5000);
  const now = useNow();
  const locations = [...(locs.data ?? [])].sort((a, b) => Number(a.reachable) - Number(b.reachable) || a.name.localeCompare(b.name));

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Zdroje a integrace</h1>
          <p className="muted">Odkud backend bere řetězce a co k nim doplňuje.</p>
        </div>
      </div>
      <ErrorBox error={comps.error} />
      <div className="comp-grid">
        {(comps.data ?? []).map((c) => (
          <section key={`${c.role}-${c.id}`} className={`card comp ${c.ok ? "h-ok" : "h-problem"}`}>
            <div className="card-head">
              <h2>{TYPE_LABEL[c.type] ?? c.type}</h2>
              <span className={`pill ${c.ok ? "h-ok" : "h-problem"}`}>
                <span className="dot" aria-hidden="true" />
                {c.ok ? "OK" : "Chyba"}
              </span>
            </div>
            <p className="muted small">
              {c.role === "source" ? "Zdroj řetězců" : "Integrace"} · <span className="mono">{c.id}</span> · úspěšně {ago(c.lastSuccess, now)}
            </p>
            {c.message && <p className="small">{c.message}</p>}
            {Object.keys(c.details).length > 0 && (
              <dl className="kv">
                {Object.entries(c.details).map(([k, v]) => (
                  <Fragment key={k}>
                    <dt>{k}</dt>
                    <dd className="mono">{Array.isArray(v) ? v.join(", ") : String(v)}</dd>
                  </Fragment>
                ))}
              </dl>
            )}
          </section>
        ))}
      </div>

      <section className="card">
        <h2>Lokality</h2>
        {locations.length === 0 ? (
          <Empty>Žádné lokality.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Lokalita</th><th>Typ</th><th>Adresa</th><th>Prostředí</th><th>Řetězce</th><th>Výstrahy</th><th>Stav</th>
                </tr>
              </thead>
              <tbody>
                {locations.map((l) => (
                  <tr key={l.id}>
                    <td className="mono"><a href={`#/?location=${encodeURIComponent(l.name)}`}>{l.name}</a></td>
                    <td><Icon name={l.kind === "docker-host" ? "box" : "server"} size={14} /> {l.kind === "docker-host" ? "Docker (Komodo)" : "VM"}</td>
                    <td className="mono">{l.address ?? "–"}</td>
                    <td>{l.environment ?? "–"}</td>
                    <td>{l.chainCount}</td>
                    <td>{l.alerts.length || "–"}</td>
                    <td>
                      {l.reachable ? (
                        <span className="state s-ok">Dostupná</span>
                      ) : (
                        <span className="state s-problem" title={l.error ?? ""}>Nedostupná · {ago(l.lastSeen, now)}</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
