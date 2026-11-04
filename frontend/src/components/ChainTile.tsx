import type { Chain } from "../api/types";
import { CHAIN_STATE, HEALTH, KIND, ago, num } from "../lib/format";
import { Badge, HealthPill, Icon, MiniFlow } from "./ui";

/** Jedna dlaždice = jeden řetězec. Lokalita je součástí dlaždice, ne nadřazená skupina. */
export function ChainTile({ chain, now }: { chain: Chain; now: number }) {
  const loc = chain.location;
  const metric = chain.metrics.find((m) => m.value !== null);
  const worstAlert = chain.alerts[0];
  return (
    <a className={`tile ${HEALTH[chain.health].cls}`} href={`#/chains/${encodeURIComponent(chain.id)}`}
      aria-label={`${chain.name} na ${loc.name}, ${HEALTH[chain.health].label}`}>
      <div className="tile-head">
        <div className="tile-title">
          <span className="tile-name">{chain.name}</span>
          <span className="tile-loc">
            <Icon name={chain.kind === "container" ? "box" : "server"} size={14} />
            <span className="mono">{loc.name}</span>
            {loc.address && <span className="muted">{loc.address}</span>}
          </span>
        </div>
        <HealthPill health={chain.health} />
      </div>

      <MiniFlow apps={chain.apps} />

      <div className="tile-meta">
        <Badge>{KIND[chain.kind]}</Badge>
        {loc.environment && <Badge>{loc.environment}</Badge>}
        <span className="muted">{chain.stale ? "data nejsou aktuální" : CHAIN_STATE[chain.state]}</span>
      </div>

      {worstAlert && (
        <div className="tile-alert" title={worstAlert.name}>
          <Icon name="alert" size={14} />
          <span className="ellipsis">{worstAlert.name}</span>
          {chain.alerts.length > 1 && <span className="muted">+{chain.alerts.length - 1}</span>}
        </div>
      )}

      <div className="tile-foot">
        <span>{chain.apps.length} {chain.apps.length === 1 ? "aplikace" : chain.apps.length < 5 ? "aplikace" : "aplikací"}</span>
        {metric ? (
          <span className="mono" title={metric.label}>
            {num(metric.value, metric.unit)}
          </span>
        ) : (
          <span className="muted">{chain.stale ? `naposledy ${ago(chain.lastSeen, now)}` : ""}</span>
        )}
      </div>
    </a>
  );
}
