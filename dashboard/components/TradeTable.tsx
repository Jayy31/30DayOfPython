import type { Trade } from "@/lib/types";
import { num, signed, pnlClass, time } from "@/lib/format";

function reasonText(t: Trade): string {
  const r = t.reason_log as Record<string, any>;
  const lines: string[] = [];
  if (r.regime) lines.push(`Regime: ${r.regime.label} (conf ${r.regime.confidence}) ${(r.regime.drivers || []).join("; ")}`);
  if (r.bias) lines.push(`Bias: ${r.bias.label} ${r.bias.score}`);
  if (r.location?.zone_price) lines.push(`Location: ${r.location.zone_price} [${(r.location.sources || []).join(", ")}]`);
  if (r.trigger) lines.push(`Trigger: strength ${r.trigger.strength} - ${(r.trigger.evidence || []).join("; ")}`);
  if (r.strike?.strike) lines.push(`Strike: ${r.strike.strike} ${r.strike.option_type} delta ${r.strike.delta} IV ${r.strike.iv}`);
  if (r.probability) lines.push(`Probability: ${r.probability.p} [${r.probability.label}, ${r.probability.model_version ?? ""}] confidence ${r.confidence ?? "–"}/5`);
  if (r.plan) lines.push(`Plan: entry ${r.plan.entry} SL ${r.plan.sl} T1 ${r.plan.target_1} T1.5 ${r.plan.target_2}; trail ${r.plan.trail}`);
  if ((r.events || []).length) lines.push(`Events: ${r.events.join(", ")}`);
  if (r.exit) lines.push(`Exit: ${r.exit.outcome} ${r.exit.net_pnl} (${r.exit.r_multiple}R)`);
  if (r.review) lines.push(`Review: ${r.review}`);
  return lines.join("\n") || "No reason log";
}

export default function TradeTable({ trades }: { trades: Trade[] }) {
  if (!trades.length) return <p className="muted">No trades in this range. No valid setup = no trade.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Day</th>
            <th>#</th>
            <th>Index</th>
            <th>Setup</th>
            <th>Contract</th>
            <th className="num">Lots</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>Outcome</th>
            <th className="num">Net P&amp;L</th>
            <th className="num">R</th>
            <th className="num">P</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.id}>
              <td>{t.day}</td>
              <td>{t.trade_no}</td>
              <td>{t.index}</td>
              <td>{t.setup}</td>
              <td>
                {t.strike} {t.option_type} <span className="muted">{t.expiry}</span>
              </td>
              <td className="num">{t.lots}</td>
              <td>
                {time(t.entry_ts)} @ {num(t.entry_price)}
              </td>
              <td>
                {time(t.exit_ts)} @ {num(t.avg_exit_price)}
              </td>
              <td>{t.outcome ?? "open"}</td>
              <td className={`num ${pnlClass(t.net_pnl)}`}>{signed(t.net_pnl)}</td>
              <td className={`num ${pnlClass(t.r_multiple)}`}>{num(t.r_multiple)}</td>
              <td className="num" title="uncalibrated (priors) until 100+ trades">
                {num(t.probability)}
              </td>
              <td className="wrap">
                <details>
                  <summary>Reason log &amp; legs</summary>
                  <pre>{reasonText(t)}</pre>
                  <pre>
                    {t.legs.map((l) => `${time(l.ts)} ${l.side} ${l.lots} lot(s) @ ${l.price} - ${l.reason} (charges ₹${l.charges})`).join("\n")}
                    {`\nCharges total ₹${t.charges}${t.tags.length ? ` · tags: ${t.tags.join(", ")}` : ""}`}
                  </pre>
                </details>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
