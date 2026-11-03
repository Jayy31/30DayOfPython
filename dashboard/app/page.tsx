import AutoRefresh from "@/components/AutoRefresh";
import ErrorCard from "@/components/ErrorCard";
import TradeTable from "@/components/TradeTable";
import { api, ApiError } from "@/lib/api";
import { money, num, pnlClass, signed, time } from "@/lib/format";
import type { IndexSnap, Today } from "@/lib/types";

export const dynamic = "force-dynamic";

function IndexCard({ name, s }: { name: string; s: IndexSnap }) {
  return (
    <div className="card">
      <h2>
        {name} <span className="muted">{num(s.spot)}</span>
      </h2>
      <dl className="kv">
        <dt>Regime</dt>
        <dd>
          {s.regime ? `${s.regime.label} (${num(s.regime.confidence)})` : "–"}
        </dd>
        <dt>Bias</dt>
        <dd>{s.bias ? `${s.bias.label} ${s.bias.score >= 0 ? "+" : ""}${num(s.bias.score)}` : "–"}</dd>
        <dt>Trigger</dt>
        <dd>{s.trigger ? `${s.trigger.direction > 0 ? "up" : s.trigger.direction < 0 ? "down" : "none"} ${num(s.trigger.strength)}` : "–"}</dd>
        <dt>Opening</dt>
        <dd>{s.opening ?? "–"}</dd>
        <dt>VWAP</dt>
        <dd>{num(s.vwap)}</dd>
        <dt>Opening range</dt>
        <dd>
          {num(s.or_low)} – {num(s.or_high)}
        </dd>
        <dt>Put / call wall</dt>
        <dd>
          {num(s.put_wall, 0)} / {num(s.call_wall, 0)} <span className="muted">PCR {num(s.pcr)}</span>
        </dd>
        <dt>Dealer gamma</dt>
        <dd>{s.gex ? `${s.gex.regime}${s.gex.flip ? `, flip ${num(s.gex.flip, 0)}` : ""}` : "–"}</dd>
        <dt>ATM straddle</dt>
        <dd>{s.straddle ? `${s.straddle.label}, move ${num(s.straddle.day_implied_move, 0)}, used ${num(s.straddle.range_used)}x` : "–"}</dd>
        <dt>Futures OI</dt>
        <dd>{s.futures_oi.replace("_", " ")}</dd>
      </dl>
      {s.zones.length > 0 && (
        <p className="muted" style={{ marginBottom: 0 }}>
          Zones: {s.zones.map((z) => `${num(z.price, 0)} [${z.sources.join("/")}]`).join(" · ")}
        </p>
      )}
    </div>
  );
}

export default async function TodayPage() {
  let data: Today;
  try {
    data = await api<Today>("/api/today");
  } catch (e) {
    return <ErrorCard message={e instanceof ApiError ? e.message : "Unexpected error"} />;
  }
  const live = data.live;
  const ot = live?.open_trade;
  return (
    <>
      <AutoRefresh seconds={10} />
      <h1>Today {live?.day ? <span className="muted">· {live.day.date}</span> : null}</h1>
      <div className="tiles">
        <div className="card tile">
          <div className="label">Capital</div>
          <div className="value">{money(live?.capital)}</div>
          <div className="sub">mode: {data.mode}</div>
        </div>
        <div className="card tile">
          <div className="label">Today net P&amp;L</div>
          <div className={`value ${pnlClass(live?.day?.net_pnl)}`}>{signed(live?.day?.net_pnl ?? 0)}</div>
          <div className="sub">{live?.day?.trades ?? 0} of 2 trades</div>
        </div>
        <div className="card tile">
          <div className="label">Signals evaluated</div>
          <div className="value">{live?.day?.signals ?? 0}</div>
          <div className="sub">feed gaps: {live?.day?.feed_gaps ?? 0}</div>
        </div>
        <div className="card tile">
          <div className="label">Feed</div>
          <div className="value status">
            <span className={`dot ${!live ? "warning" : live.feed_stale ? "critical" : "good"}`} aria-hidden="true" />
            {!live ? "Not running" : live.feed_stale ? "Silent" : "Live"}
          </div>
          <div className="sub">last tick {time(live?.last_tick)}</div>
        </div>
      </div>

      <div className="section card">
        <h2>Open trade</h2>
        {ot ? (
          <dl className="kv">
            <dt>Contract</dt>
            <dd>
              {ot.index} {ot.strike} {ot.option_type} · {ot.setup} ({ot.direction})
            </dd>
            <dt>Lots</dt>
            <dd>
              {ot.open_lots} open of {ot.lots}
            </dd>
            <dt>Entry</dt>
            <dd>
              {num(ot.entry_price)} at {time(ot.entry_ts)}
            </dd>
            <dt>Bid now</dt>
            <dd>{num(ot.bid)}</dd>
            <dt>Stop</dt>
            <dd>
              {num(ot.sl)} {ot.trailed ? "(trailing)" : ot.stage === 2 ? "(+20% lock)" : ot.stage === 1 ? "(cost +5%)" : "(initial)"}
            </dd>
            <dt>Targets</dt>
            <dd>
              1R {num(ot.target_1)} · 1.5R {num(ot.target_2)}
            </dd>
            <dt>Mark-to-market</dt>
            <dd className={pnlClass(ot.mtm)}>{signed(ot.mtm)}</dd>
            <dt>Probability</dt>
            <dd title="uncalibrated (priors)">{num(ot.probability)} · uncalibrated</dd>
          </dl>
        ) : (
          <p className="muted">No open trade.</p>
        )}
      </div>

      {live && (
        <div className="section grid">
          {Object.entries(live.indices).map(([name, s]) => (
            <IndexCard key={name} name={name} s={s} />
          ))}
        </div>
      )}

      <div className="section card">
        <h2>Trades today</h2>
        <TradeTable trades={data.trades_today} />
      </div>

      <div className="section card">
        <h2>Recent signals</h2>
        {live?.signals.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Index</th>
                  <th>Setup</th>
                  <th>Dir</th>
                  <th className="num">Score</th>
                  <th className="num">P</th>
                  <th>Decision</th>
                </tr>
              </thead>
              <tbody>
                {live.signals.slice(0, 25).map((s, i) => (
                  <tr key={i}>
                    <td>{time(s.ts)}</td>
                    <td>{s.index}</td>
                    <td>{s.setup}</td>
                    <td>{s.direction}</td>
                    <td className="num">{num(s.score)}</td>
                    <td className="num" title={s.probability_label ?? ""}>
                      {num(s.probability)}
                    </td>
                    <td className="wrap">{s.taken ? <strong>Taken</strong> : s.reasons.join("; ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="muted">No signals yet.</p>
        )}
      </div>
    </>
  );
}
