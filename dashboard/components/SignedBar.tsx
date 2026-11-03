import { signed } from "@/lib/format";

// Diverging bar around a zero axis: blue = profit, red = loss. The signed value is always
// printed beside it, so the colour never carries the meaning alone.
export default function SignedBar({ value, max }: { value: number; max: number }) {
  const w = max > 0 ? Math.min(Math.abs(value) / max, 1) * 50 : 0;
  const pos = value >= 0;
  return (
    <div className="bar-cell">
      <div className="bar-track" aria-hidden="true">
        <div className="axis" />
        {value !== 0 && <div className={`bar ${pos ? "pos" : "neg"}`} style={pos ? { left: "50%", width: `${w}%` } : { right: "50%", width: `${w}%` }} />}
      </div>
      <span className={pos ? "pos" : "neg"} style={{ minWidth: 96, textAlign: "right" }}>
        {signed(value)}
      </span>
    </div>
  );
}
