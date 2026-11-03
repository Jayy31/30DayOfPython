import ErrorCard from "@/components/ErrorCard";
import TradeTable from "@/components/TradeTable";
import { api, ApiError } from "@/lib/api";
import { pnlClass, signed } from "@/lib/format";
import type { Trade } from "@/lib/types";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ start?: string; end?: string }> };

export default async function TradesPage({ searchParams }: Props) {
  const { start, end } = await searchParams;
  let data: { start: string; end: string; trades: Trade[] };
  try {
    data = await api("/api/trades", { start, end });
  } catch (e) {
    return <ErrorCard message={e instanceof ApiError ? e.message : "Unexpected error"} />;
  }
  const net = data.trades.reduce((s, t) => s + t.net_pnl, 0);
  return (
    <>
      <h1>Trades</h1>
      <form className="filters">
        <label>
          From
          <input type="date" name="start" defaultValue={data.start} />
        </label>
        <label>
          To
          <input type="date" name="end" defaultValue={data.end} />
        </label>
        <button type="submit">Apply</button>
        <span className="muted">
          {data.trades.length} trades · net <span className={pnlClass(net)}>{signed(net)}</span>
        </span>
      </form>
      <div className="card">
        <TradeTable trades={[...data.trades].reverse()} />
      </div>
    </>
  );
}
