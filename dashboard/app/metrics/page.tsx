import CapitalCurve from "@/components/CapitalCurve";
import ErrorCard from "@/components/ErrorCard";
import SignedBar from "@/components/SignedBar";
import { api, ApiError } from "@/lib/api";
import { money, num, pct, pnlClass, signed } from "@/lib/format";
import type { MetricsResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ start?: string; end?: string }> };

const LABELS: Record<string, string> = {
  setup: "By setup",
  index: "By index",
  time_window: "By time window",
  regime: "By regime",
  vix_band: "By VIX band",
  event: "By event tag",
  expiry_day: "Expiry vs non-expiry day",
  trade_no: "Trade 1 vs trade 2",
  outcome: "By outcome",
};

export default async function MetricsPage({ searchParams }: Props) {
  const { start, end } = await searchParams;
  let d: MetricsResponse;
  try {
    d = await api<MetricsResponse>("/api/metrics", { start, end });
  } catch (e) {
    return <ErrorCard message={e instanceof ApiError ? e.message : "Unexpected error"} />;
  }
  const m = d.metrics;
  const judgeable = d.sample.trades_total >= d.sample.min_to_judge;
  return (
    <>
      <h1>Metrics</h1>
      <form className="filters">
        <label>
          From
          <input type="date" name="start" defaultValue={d.start} />
        </label>
        <label>
          To
          <input type="date" name="end" defaultValue={d.end} />
        </label>
        <button type="submit">Apply</button>
      </form>
      {!judgeable && (
        <div className="card" role="note" style={{ marginBottom: 12 }}>
          <span className="status">
            <span className="dot warning" aria-hidden="true" />
            <strong>Too few trades to judge:</strong> {d.sample.trades_total} of {d.sample.min_to_judge}+ needed. Probabilities are {d.probabilities}.
          </span>
        </div>
      )}
      <div className="tiles">
        <div className="card tile">
          <div className="label">Net P&amp;L</div>
          <div className={`value ${pnlClass(m.net_pnl)}`}>{signed(m.net_pnl)}</div>
          <div className="sub">capital {money(m.end_capital)}</div>
        </div>
        <div className="card tile">
          <div className="label">Expectancy / trade</div>
          <div className={`value ${pnlClass(m.expectancy)}`}>{signed(m.expectancy)}</div>
          <div className="sub">target: above zero</div>
        </div>
        <div className="card tile">
          <div className="label">Profit factor</div>
          <div className="value">{m.profit_factor == null ? "–" : num(m.profit_factor)}</div>
          <div className="sub">target: above 1.3</div>
        </div>
        <div className="card tile">
          <div className="label">Win rate</div>
          <div className="value">{pct(m.win_rate)}</div>
          <div className="sub">
            {m.wins}W / {m.losses}L of {m.trades}
          </div>
        </div>
        <div className="card tile">
          <div className="label">Avg win / avg loss</div>
          <div className="value" style={{ fontSize: 18 }}>
            {money(m.avg_win)} / {money(m.avg_loss)}
          </div>
          <div className="sub">avg R {num(m.avg_r)}</div>
        </div>
        <div className="card tile">
          <div className="label">Max drawdown</div>
          <div className="value">{money(m.max_drawdown)}</div>
          <div className="sub">
            {num(m.max_drawdown_pct, 1)}% · longest losing streak {m.longest_losing_streak}
          </div>
        </div>
      </div>

      <div className="section card">
        <h2>Capital curve</h2>
        {m.capital_curve && m.capital_curve.length ? (
          <CapitalCurve start={d.starting_capital} points={m.capital_curve} />
        ) : (
          <p className="muted">No trades yet.</p>
        )}
      </div>

      <div className="section grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(420px, 100%), 1fr))" }}>
        {Object.entries(d.breakdowns).map(([key, groups]) => {
          const rows = Object.entries(groups);
          const max = Math.max(0, ...rows.map(([, g]) => Math.abs(g.net_pnl)));
          return (
            <div className="card" key={key}>
              <h2>{LABELS[key] ?? key}</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Group</th>
                      <th className="num">Trades</th>
                      <th className="num">Win</th>
                      <th className="num">Exp.</th>
                      <th>Net P&amp;L</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(([g, v]) => (
                      <tr key={g}>
                        <td>{g.replaceAll("_", " ")}</td>
                        <td className="num">{v.trades}</td>
                        <td className="num">{pct(v.win_rate)}</td>
                        <td className={`num ${pnlClass(v.expectancy)}`}>{signed(v.expectancy)}</td>
                        <td>
                          <SignedBar value={v.net_pnl} max={max} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
