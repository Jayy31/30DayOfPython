import ErrorCard from "@/components/ErrorCard";
import { api, ApiError } from "@/lib/api";
import type { Report } from "@/lib/types";

export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ kind?: string }> };
const KINDS = ["premarket", "postmarket", "weekly", "monthly"];

export default async function ReportsPage({ searchParams }: Props) {
  const { kind } = await searchParams;
  const k = kind && KINDS.includes(kind) ? kind : undefined;
  let data: { reports: Report[] };
  try {
    data = await api("/api/reports", { kind: k, limit: "60" });
  } catch (e) {
    return <ErrorCard message={e instanceof ApiError ? e.message : "Unexpected error"} />;
  }
  return (
    <>
      <h1>Reports</h1>
      <form className="filters">
        <label>
          Type
          <select name="kind" defaultValue={k ?? ""}>
            <option value="">All</option>
            {KINDS.map((x) => (
              <option key={x} value={x}>
                {x}
              </option>
            ))}
          </select>
        </label>
        <button type="submit">Apply</button>
      </form>
      {data.reports.length ? (
        data.reports.map((r, i) => (
          <details className="card" key={`${r.kind}-${r.period}`} open={i === 0} style={{ marginBottom: 8 }}>
            <summary>
              <strong>{r.kind}</strong> <span className="muted">· {r.period}</span>
            </summary>
            <pre>{r.body}</pre>
          </details>
        ))
      ) : (
        <p className="muted">No reports yet.</p>
      )}
    </>
  );
}
