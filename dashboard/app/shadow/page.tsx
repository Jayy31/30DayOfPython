import ErrorCard from "@/components/ErrorCard";
import SignedBar from "@/components/SignedBar";
import { api, ApiError } from "@/lib/api";
import { pnlClass, signed } from "@/lib/format";
import type { ShadowResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ start?: string; end?: string }> };

const DESC: Record<string, string> = {
  weekly_profit_cap: "Stop for the week at +₹3,000–4,000",
  weekly_sl_pause: "Stop for the week after 3 SLs",
  monthly_drawdown_cap: "Pause after a 30% fall from the month's peak",
  half_sizing: "Every trade at ~50% of capital (exit ladder re-run)",
  index_switch_tag: "Skip a same-direction trade on the other index after an SL",
  trail_1min: "Trail on 1-minute swing lows",
  trail_5min: "Trail on 5-minute swing lows",
};

export default async function ShadowPage({ searchParams }: Props) {
  const { start, end } = await searchParams;
  let d: ShadowResponse;
  try {
    d = await api<ShadowResponse>("/api/shadow", { start, end });
  } catch (e) {
    return <ErrorCard message={e instanceof ApiError ? e.message : "Unexpected error"} />;
  }
  const max = Math.max(0, ...d.rules.map((r) => Math.abs(r.difference)));
  return (
    <>
      <h1>Shadow rules</h1>
      <p className="muted">
        Simulated in parallel on the same trades; they never block real paper trading. Actual P&amp;L for {d.trades} trades:{" "}
        <span className={pnlClass(d.actual_pnl)}>{signed(d.actual_pnl)}</span>. {d.note}.
      </p>
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
      <div className="card">
        {d.rules.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>What it simulates</th>
                  <th className="num">P&amp;L if active</th>
                  <th>Difference vs actual</th>
                  <th className="num">Trades changed</th>
                </tr>
              </thead>
              <tbody>
                {d.rules.map((r) => (
                  <tr key={r.rule}>
                    <td>{r.rule}</td>
                    <td className="wrap muted">{DESC[r.rule] ?? ""}</td>
                    <td className={`num ${pnlClass(r.simulated_pnl)}`}>{signed(r.simulated_pnl)}</td>
                    <td>
                      <SignedBar value={r.difference} max={max} />
                    </td>
                    <td className="num">
                      {r.trades_changed} / {r.trades_covered}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No shadow results yet. They are computed when the weekly or monthly report runs.</p>
        )}
      </div>
    </>
  );
}
