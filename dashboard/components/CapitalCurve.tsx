"use client";

import { useMemo, useRef, useState } from "react";

type Point = { label: string; value: number };

const W = 720;
const H = 240;
const PAD = { l: 64, r: 16, t: 12, b: 28 };
const fmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

// Single-series line (no legend: the card title names it). Crosshair + tooltip on hover/focus.
export default function CapitalCurve({ start, points }: { start: number; points: [string, number][] }) {
  const data: Point[] = useMemo(() => [{ label: "start", value: start }, ...points.map(([label, value]) => ({ label, value }))], [start, points]);
  const [hover, setHover] = useState<number | null>(null);
  const ref = useRef<SVGSVGElement>(null);

  const values = data.map((d) => d.value);
  let lo = Math.min(...values, start);
  let hi = Math.max(...values, start);
  if (hi === lo) {
    hi += 1;
    lo -= 1;
  }
  const padY = (hi - lo) * 0.08;
  lo -= padY;
  hi += padY;
  const x = (i: number) => PAD.l + (data.length === 1 ? 0 : (i / (data.length - 1)) * (W - PAD.l - PAD.r));
  const y = (v: number) => PAD.t + (1 - (v - lo) / (hi - lo)) * (H - PAD.t - PAD.b);
  const ticks = [lo + padY, (lo + hi) / 2, hi - padY];
  const path = data.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.value).toFixed(1)}`).join("");

  function onMove(e: React.PointerEvent<SVGSVGElement>) {
    const box = ref.current?.getBoundingClientRect();
    if (!box) return;
    const px = ((e.clientX - box.left) / box.width) * W;
    const i = Math.round(((px - PAD.l) / (W - PAD.l - PAD.r)) * (data.length - 1));
    setHover(Math.max(0, Math.min(data.length - 1, i)));
  }

  const h = hover != null ? data[hover] : null;
  return (
    <div className="chart">
      <svg
        ref={ref}
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Capital curve from ₹${fmt.format(start)} to ₹${fmt.format(values[values.length - 1])} over ${points.length} trades`}
        tabIndex={0}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") setHover((v) => Math.min(data.length - 1, (v ?? -1) + 1));
          if (e.key === "ArrowLeft") setHover((v) => Math.max(0, (v ?? data.length) - 1));
        }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} stroke="var(--grid)" />
            <text x={PAD.l - 8} y={y(t)} dy="0.32em" textAnchor="end" fontSize="11" fill="var(--text-muted)">
              ₹{fmt.format(t)}
            </text>
          </g>
        ))}
        <line x1={PAD.l} x2={W - PAD.r} y1={y(start)} y2={y(start)} stroke="var(--text-muted)" strokeDasharray="3 4" />
        <path d={path} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
        {data.length <= 60 &&
          data.map((d, i) => <circle key={i} cx={x(i)} cy={y(d.value)} r={3} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2} />)}
        <text x={PAD.l} y={H - 8} fontSize="11" fill="var(--text-muted)">
          start
        </text>
        <text x={W - PAD.r} y={H - 8} textAnchor="end" fontSize="11" fill="var(--text-muted)">
          trade {points.length}
        </text>
        {h && hover != null && (
          <g pointerEvents="none">
            <line x1={x(hover)} x2={x(hover)} y1={PAD.t} y2={H - PAD.b} stroke="var(--text-muted)" />
            <circle cx={x(hover)} cy={y(h.value)} r={5} fill="var(--series-1)" stroke="var(--surface-1)" strokeWidth={2} />
          </g>
        )}
      </svg>
      {h && hover != null && (
        <div className="tip" style={{ left: `${(x(hover) / W) * 100}%`, top: 0, transform: `translateX(${hover > data.length / 2 ? "-105%" : "5%"})` }}>
          <div className="muted">{h.label === "start" ? "Starting capital" : h.label.replace("#", " · trade ")}</div>
          <strong>₹{fmt.format(h.value)}</strong>
        </div>
      )}
    </div>
  );
}
