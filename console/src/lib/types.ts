// Shapes that travel from the server (src/lib/snapshot.ts) to the browser. Every number the user reads is already a formatted string.
export type SideCode = "LONG" | "SHORT";
export type Tone = "ok" | "err" | "warn" | "dim";
export type PnlSign = -1 | 0 | 1;

export interface RunnerView {
  up: boolean;                 // legacy runner.json fresh (kept for compatibility)
  note: string;
  updatedAt: string;
  teamRunning: boolean;
  pid: number | null;
  nextAuto: string | null;
  waitS: number;
  kafkaUp: boolean;
  status: string;
  portfolioAt: string;
  kafkaAt: string;
}

export interface MemberView { thread: string; emoji: string; name: string; role: string; model: string; last: string; error: boolean; active: boolean }
export interface ChatLine { time: string; name: string; text: string }
export interface CostRow { model: string; calls: number; tin: string; tout: string; usd: string }
export interface LogRow { when: string; icon: string; kind: "said" | "llm" | "warn" | "error"; text: string }

export interface PlanRowView {
  n: string; sideCode: SideCode; side: string; coin: string; signal: string; score: string; price: string; rsi1h: string; rsi4h: string;
  ageDays: string; trend1d: string; ddAth: string; vol30: string; r7: string; trend: string; png: string | null;
}
export interface PlanView {
  key: string; at: number;
  header: string; day: string; paused: boolean; rows: PlanRowView[]; info: string; narrative: string;
  shortCoins: string[];
  runId: string;
}

export interface PfShortView {
  sideCode: SideCode; side: string; coin: string; signal: string; score: string; rsi1h: string; rsi4h: string; ageDays: string;
  trend1d: string; ddAth: string; qty: string; price: string; liq: string; margin: string; pnl: string; pnlSign: PnlSign;
  funding: string; held: string; state: string; stateOk: boolean;
  id: string; closable: boolean;
  // Live V22 position metadata from cache/agy-positions.json
  setup: string; exitMode: string; stopPx: string; tpPx: string; stage: string; mfePct: string; maePct: string;
}
export interface PfClosedView {
  sideCode: SideCode; side: string; coin: string; qty: string; entry: string; opened: string; closed: string; why: string;
  pnl: string; pnlSign: PnlSign; funding: string; fees: string; alertPnl: string;
}
export interface PfOrderView { when: string; kind: string; symbol: string; qty: string; usdt: string; price: string; status: string }
export interface PfLogView { when: string; run: string; text: string }
export interface PortfolioView {
  key: string; hasGood: boolean;
  mode: string; at: string; rule: string; error: string; spot: string;
  equity: string; available: string; unrealized: string; unrealizedSign: PnlSign; realized: string; realizedSign: PnlSign; lot: string; hasLot: boolean;
  shorts: PfShortView[]; closed: PfClosedView[]; orders: PfOrderView[]; log: PfLogView[];
}

export interface AgyRoundRow {
  at: string;
  ok: boolean;
  strategy: string;
  regime: string;
  complete1h: string;
  cands: number;
  opened: number;
  closed: number;
  heat: string;
  equity: string;
  openCount: number;
}

export interface AgyGuardRow {
  at: string;
  ok: boolean;
  checked: number;
  closedCount: number;
  closedText: string;
  cbTripped: boolean;
  equity: string;
}

export interface AgyStatusView {
  guardUp: boolean;
  guardAt: string;
  strategy: string;
  regime: string;
  btcPrice: string;
  b1d: string;
  b1h: string;
  lastRoundAt: string;
  nextRoundIso: string;
  cbTripped: boolean;
  cbBaseline: string;
  cbDdUsdt: string;
  cbDdPct: string;
  cbDdSign: PnlSign;
  cbReason: string;
  openHeat: string;
  maxHeat: string | null;
  dailyUsed: number;
  dailyCap: number | null;
  maxOpen: number | null;      // null = unlimited: new opens bounded by available >= minAvailUsdt
  maxLong: number | null;
  leverage: number;
  marginUsdt: string;
  lotUsdt: string;
  minAvailUsdt: string;
  affordMore: number | null;   // floor(available / minAvailUsdt), null = unknown
  cbAuto: boolean;
  cbRefEquity: string;         // formatted equity the drawdown is measured from (peak when auto)
  cbLimitUsdt: string;
  cbLimitPct: string;
  hourlyMaxOpens: number;
  enforceRegimeGate: boolean;
  enforceCooldown: boolean;
  recentRounds: AgyRoundRow[];
  recentGuards: AgyGuardRow[];
}

export interface BotDetailView {
  thread: string; emoji: string; name: string; role: string; model: string; listens: string; sends: string;
  status: string; tone: Tone; stats: string; timeline: LogRow[];
}

export interface Snapshot {
  serverNow: string;
  runner: RunnerView;
  agy: AgyStatusView;
  members: MemberView[];
  chat: ChatLine[];
  cost: { today: string; month: string; rows: CostRow[]; unknownModel: boolean };
  problems: string[];
  plan: PlanView | null;
  pf: PortfolioView | null;
  detail: BotDetailView | null;
}

export interface KafkaRow { when: string; topic: string; offset: number; summary: string; json: string }
export interface KafkaView {
  key: string; at: string; atMs: number | null; error: string; newest: string;
  topics: { topic: string; count: string }[]; rows: KafkaRow[];
  runnerAt: string;
}

export type RequestKind = "run" | "stop" | "portfolio" | "kafka";
export const REQUEST_KINDS: readonly RequestKind[] = ["run", "stop", "portfolio", "kafka"];
