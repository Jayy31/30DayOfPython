export type Leg = { ts: string; side: string; lots: number; price: number; reason: string; charges: number };

export type Trade = {
  id: string;
  day: string;
  trade_no: number;
  setup: string;
  index: string;
  strike: number;
  option_type: string;
  expiry: string;
  symbol: string;
  direction: string;
  lots: number;
  entry_ts: string;
  entry_price: number;
  exit_ts: string | null;
  avg_exit_price: number | null;
  outcome: string | null;
  r_multiple: number;
  net_pnl: number;
  gross_pnl: number;
  charges: number;
  probability: number | null;
  tags: string[];
  legs: Leg[];
  reason_log: Record<string, unknown>;
};

export type Signal = {
  ts: string;
  index: string;
  setup: string;
  direction: string;
  score: number | null;
  probability: number | null;
  probability_label: string | null;
  taken: boolean;
  reasons: string[];
};

export type IndexSnap = {
  spot: number | null;
  vwap: number | null;
  or_high: number | null;
  or_low: number | null;
  regime: { label: string; confidence: number; drivers: string[] } | null;
  bias: { label: string; score: number } | null;
  trigger: { direction: number; strength: number } | null;
  zones: { price: number; sources: string[] }[];
  call_wall: number | null;
  put_wall: number | null;
  pcr: number | null;
  gex: { regime: string; flip: number | null } | null;
  straddle: { label: string; expansion: number; day_implied_move: number; range_used: number } | null;
  opening: string | null;
  futures_oi: string;
};

export type OpenTrade = {
  symbol: string;
  index: string;
  strike: number;
  option_type: string;
  direction: string;
  setup: string;
  lots: number;
  open_lots: number;
  entry_price: number;
  entry_ts: string;
  sl: number;
  target_1: number;
  target_2: number;
  stage: number;
  trailed: boolean;
  bid: number | null;
  mtm: number;
  probability: number | null;
};

export type Live = {
  last_tick: string | null;
  feed_stale: boolean;
  capital: number;
  open_trade: OpenTrade | null;
  day: { date: string; trades: number; signals: number; feed_gaps: number; net_pnl: number } | null;
  indices: Record<string, IndexSnap>;
  signals: Signal[];
};

export type Today = { mode: string; live: Live | null; trades_today: Trade[] };

export type Metrics = {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  profit_factor: number | null;
  net_pnl: number;
  avg_r: number;
  max_drawdown: number;
  max_drawdown_pct: number;
  longest_losing_streak: number;
  end_capital: number;
  capital_curve?: [string, number][];
};

export type MetricsResponse = {
  start: string;
  end: string;
  starting_capital: number;
  metrics: Metrics;
  breakdowns: Record<string, Record<string, Metrics>>;
  sample: { trades_total: number; min_to_judge: number };
  probabilities: string;
};

export type ShadowResponse = {
  start: string;
  end: string;
  actual_pnl: number;
  trades: number;
  rules: { rule: string; simulated_pnl: number; difference: number; trades_changed: number; trades_covered: number }[];
  note: string;
};

export type Report = { kind: string; period: string; body: string; data: Record<string, unknown> };
