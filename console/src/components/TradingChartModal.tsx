"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  ColorType,
  createChart,
  createTextWatermark,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ITextWatermarkPluginApi,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import {
  bkkCrosshairTime,
  bkkTickLabel,
  calcEma,
  calcPricePrecision,
  calcRsi,
  calcSma,
  coinFromSymbol,
  extractEntryPrice,
  formatChartPrice,
  formatVolume,
  KLINE_INTERVALS,
  parseChartPrice,
  type Candle,
  type KlineInterval,
} from "@/lib/indicators";
import type { PfClosedView, PfShortView, PnlSign, SideCode } from "@/lib/types";
import { pnlClass, SideBadge } from "./ui";

export interface ChartCoinOption {
  symbol: string;
  coin: string;
  group: "open" | "btc" | "plan" | "other";
  sideCode?: SideCode;
  pnlSign?: PnlSign;
}

const REFRESH_MS = 10_000;
const TF_LABELS: Record<KlineInterval, string> = { "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D" };

type ThemeMode = "dark" | "light";

interface Palette {
  bg: string;
  grid: string;
  text: string;
  border: string;
  sep: string;
  sepHover: string;
  cross: string;
  crossBg: string;
  crossText: string;
  up: string;
  down: string;
  volUp: string;
  volDown: string;
  ema9: string;
  ema21: string;
  ema20: string;
  sma50: string;
  rsi: string;
  rsiMid: string;
  entry: string;
  sl: string;
  tp: string;
  liq: string;
  watermark: string;
}

const PALETTE: Record<ThemeMode, Palette> = {
  dark: {
    bg: "#071122",
    grid: "rgba(38, 63, 107, 0.34)",
    text: "#9ab0d4",
    border: "#1d3258",
    sep: "#1e355c",
    sepHover: "rgba(247, 198, 49, 0.35)",
    cross: "rgba(247, 198, 49, 0.55)",
    crossBg: "#192742",
    crossText: "#ffd666",
    up: "#26d687",
    down: "#f2556b",
    volUp: "rgba(38, 214, 135, 0.25)",
    volDown: "rgba(242, 85, 107, 0.25)",
    ema9: "#f7c631",
    ema21: "#38bdf8",
    ema20: "#c084fc",
    sma50: "#fb923c",
    rsi: "#a78bfa",
    rsiMid: "rgba(147, 166, 198, 0.35)",
    entry: "#f7c631",
    sl: "#ff5a70",
    tp: "#2ee891",
    liq: "#fb923c",
    watermark: "rgba(247, 198, 49, 0.06)",
  },
  light: {
    bg: "#fbfdff",
    grid: "rgba(159, 181, 214, 0.34)",
    text: "#2e456b",
    border: "#c5d3e8",
    sep: "#c5d3e8",
    sepHover: "rgba(0, 82, 180, 0.25)",
    cross: "rgba(0, 82, 180, 0.50)",
    crossBg: "#0047ab",
    crossText: "#ffffff",
    up: "#07854c",
    down: "#cf263b",
    volUp: "rgba(7, 133, 76, 0.22)",
    volDown: "rgba(207, 38, 59, 0.22)",
    ema9: "#d48806",
    ema21: "#0066cc",
    ema20: "#7c3aed",
    sma50: "#c2410c",
    rsi: "#6d28d9",
    rsiMid: "rgba(63, 85, 120, 0.30)",
    entry: "#0052b4",
    sl: "#c02032",
    tp: "#077040",
    liq: "#b45309",
    watermark: "rgba(0, 71, 171, 0.055)",
  },
};

function readDomTheme(): ThemeMode {
  if (typeof document === "undefined") return "dark";
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

interface HudBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  pct: number;
  ema9: number | null;
  ema21: number | null;
  ema20: number | null;
  sma50: number | null;
  rsi14: number | null;
}

interface ComputedData {
  candles: Candle[];
  precision: number;
  minMove: number;
  ema9: { time: UTCTimestamp; value: number }[];
  ema21: { time: UTCTimestamp; value: number }[];
  ema20: { time: UTCTimestamp; value: number }[];
  sma50: { time: UTCTimestamp; value: number }[];
  rsi14: { time: UTCTimestamp; value: number }[];
  byTime: Map<number, HudBar>;
  latest: HudBar | null;
}

function buildComputed(candles: Candle[]): ComputedData {
  const closes = candles.map((c) => c.close);
  const { precision, minMove } = calcPricePrecision(closes);
  const e9 = calcEma(closes, 9);
  const e21 = calcEma(closes, 21);
  const e20 = calcEma(closes, 20);
  const s50 = calcSma(closes, 50);
  const r14 = calcRsi(closes, 14);

  const ema9: { time: UTCTimestamp; value: number }[] = [];
  const ema21: { time: UTCTimestamp; value: number }[] = [];
  const ema20: { time: UTCTimestamp; value: number }[] = [];
  const sma50: { time: UTCTimestamp; value: number }[] = [];
  const rsi14: { time: UTCTimestamp; value: number }[] = [];
  const byTime = new Map<number, HudBar>();
  let latest: HudBar | null = null;

  for (let i = 0; i < candles.length; i++) {
    const c = candles[i];
    const t = c.time as UTCTimestamp;
    if (e9[i] !== null) ema9.push({ time: t, value: e9[i]! });
    if (e21[i] !== null) ema21.push({ time: t, value: e21[i]! });
    if (e20[i] !== null) ema20.push({ time: t, value: e20[i]! });
    if (s50[i] !== null) sma50.push({ time: t, value: s50[i]! });
    if (r14[i] !== null) rsi14.push({ time: t, value: r14[i]! });
    const bar: HudBar = {
      time: c.time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
      volume: c.volume,
      pct: c.open > 0 ? ((c.close - c.open) / c.open) * 100 : 0,
      ema9: e9[i],
      ema21: e21[i],
      ema20: e20[i],
      sma50: s50[i],
      rsi14: r14[i],
    };
    byTime.set(c.time, bar);
    latest = bar;
  }
  return { candles, precision, minMove, ema9, ema21, ema20, sma50, rsi14, byTime, latest };
}

function chartThemeOptions(mode: ThemeMode) {
  const p = PALETTE[mode];
  return {
    layout: {
      background: { type: ColorType.Solid, color: p.bg },
      textColor: p.text,
      fontFamily: 'var(--mono), "JetBrains Mono", Consolas, monospace',
      fontSize: 11,
      attributionLogo: false,
      panes: {
        separatorColor: p.sep,
        separatorHoverColor: p.sepHover,
        enableResize: true,
      },
    },
    grid: {
      vertLines: { color: p.grid },
      horzLines: { color: p.grid },
    },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: p.cross, width: 1 as const, style: LineStyle.Dashed, labelBackgroundColor: p.crossBg },
      horzLine: { color: p.cross, width: 1 as const, style: LineStyle.Dashed, labelBackgroundColor: p.crossBg },
    },
    rightPriceScale: { borderColor: p.border },
    timeScale: {
      borderColor: p.border,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      tickMarkFormatter: (time: Time, tickMarkType: TickMarkType) => {
        if (typeof time !== "number") return "";
        const isDate = tickMarkType <= TickMarkType.DayOfMonth;
        return bkkTickLabel(time, isDate);
      },
    },
    localization: {
      timeFormatter: (time: Time) => (typeof time === "number" ? bkkCrosshairTime(time) : ""),
    },
  };
}

interface ChartRefs {
  chart: IChartApi;
  candle: ISeriesApi<"Candlestick">;
  volume: ISeriesApi<"Histogram">;
  ema9: ISeriesApi<"Line">;
  ema21: ISeriesApi<"Line">;
  ema20: ISeriesApi<"Line">;
  sma50: ISeriesApi<"Line">;
  rsi: ISeriesApi<"Line"> | null;
  rsiLines: IPriceLine[];
  levelLines: IPriceLine[];
  watermark: ITextWatermarkPluginApi<Time>;
}

export function TradingChartModal({
  symbol,
  coins,
  position,
  closedPosition,
  onSelectSymbol,
  onClose,
}: {
  symbol: string | null;
  coins: readonly ChartCoinOption[];
  position: PfShortView | null;
  closedPosition: PfClosedView | null;
  onSelectSymbol: (sym: string) => void;
  onClose: () => void;
}) {
  const dlgRef = useRef<HTMLDialogElement>(null);
  const open = symbol !== null;

  useEffect(() => {
    const d = dlgRef.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  return (
    <dialog
      ref={dlgRef}
      className="chart-dlg"
      aria-labelledby="chart-dlg-title"
      onClose={onClose}
      onClick={(e) => { if (e.target === dlgRef.current) onClose(); }}
    >
      {symbol && (
        <TradingChartBody
          symbol={symbol}
          coins={coins}
          position={position}
          closedPosition={closedPosition}
          onSelectSymbol={onSelectSymbol}
          onClose={onClose}
        />
      )}
    </dialog>
  );
}

function TradingChartBody({
  symbol,
  coins,
  position,
  closedPosition,
  onSelectSymbol,
  onClose,
}: {
  symbol: string;
  coins: readonly ChartCoinOption[];
  position: PfShortView | null;
  closedPosition: PfClosedView | null;
  onSelectSymbol: (sym: string) => void;
  onClose: () => void;
}) {
  const coin = coinFromSymbol(symbol);
  const [interval, setIntervalTf] = useState<KlineInterval>("15m");
  const [theme, setTheme] = useState<ThemeMode>(readDomTheme);
  const [showEma921, setShowEma921] = useState(true);
  const [showEma2050, setShowEma2050] = useState(false);
  const [showRsi, setShowRsi] = useState(true);
  const [showLevels, setShowLevels] = useState(true);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [latest, setLatest] = useState<HudBar | null>(null);
  const [hoverBar, setHoverBar] = useState<HudBar | null>(null);
  const [precision, setPrecision] = useState(4);

  const stageRef = useRef<HTMLDivElement>(null);
  const refs = useRef<ChartRefs | null>(null);
  const dataRef = useRef<ComputedData | null>(null);
  const themeRef = useRef<ThemeMode>(theme);
  themeRef.current = theme;
  const showRsiRef = useRef(showRsi);
  showRsiRef.current = showRsi;
  const inflightRef = useRef<AbortController | null>(null);
  const hoverTimeRef = useRef<number | null>(null);

  // Sync instantly with <html data-theme="..."> changes (Dark / Light mode switch)
  useEffect(() => {
    setTheme(readDomTheme());
    const mo = new MutationObserver(() => setTheme(readDomTheme()));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => mo.disconnect();
  }, []);

  const attachRsiPane = useCallback((r: ChartRefs, mode: ThemeMode) => {
    if (r.rsi) return;
    const p = PALETTE[mode];
    const rsi = r.chart.addSeries(
      LineSeries,
      {
        color: p.rsi,
        lineWidth: 2,
        title: "RSI 14",
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerRadius: 3,
        priceFormat: { type: "price", precision: 1, minMove: 0.1 },
        autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
      },
      1,
    );
    const l70 = rsi.createPriceLine({ price: 70, color: p.down, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "70" });
    const l50 = rsi.createPriceLine({ price: 50, color: p.rsiMid, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: "" });
    const l30 = rsi.createPriceLine({ price: 30, color: p.up, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "30" });
    r.rsi = rsi;
    r.rsiLines = [l70, l50, l30];
    const panes = r.chart.panes();
    if (panes.length >= 2) {
      panes[0].setStretchFactor(0.72);
      panes[1].setStretchFactor(0.28);
      r.chart.priceScale("right", 1).applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 }, borderColor: p.border });
    }
    if (dataRef.current) rsi.setData(dataRef.current.rsi14);
  }, []);

  const detachRsiPane = useCallback((r: ChartRefs) => {
    if (!r.rsi) return;
    for (const l of r.rsiLines) r.rsi.removePriceLine(l);
    r.rsiLines = [];
    r.chart.removeSeries(r.rsi);
    r.rsi = null;
    if (r.chart.panes().length > 1) r.chart.removePane(1);
    const panes = r.chart.panes();
    if (panes[0]) panes[0].setStretchFactor(1);
  }, []);

  // Create lightweight-charts v5 instance once for the modal lifetime + ResizeObserver
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const mode = themeRef.current;
    const p = PALETTE[mode];
    const w = Math.max(320, el.clientWidth || 960);
    const h = Math.max(300, el.clientHeight || 520);

    const chart = createChart(el, { ...chartThemeOptions(mode), width: w, height: h });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: p.up,
      downColor: p.down,
      borderUpColor: p.up,
      borderDownColor: p.down,
      wickUpColor: p.up,
      wickDownColor: p.down,
      lastValueVisible: true,
      priceLineVisible: true,
    });
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "volume" },
      priceScaleId: "vol",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("vol", 0).applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });
    chart.priceScale("right", 0).applyOptions({ scaleMargins: { top: 0.1, bottom: 0.24 } });

    const ema9 = chart.addSeries(LineSeries, { color: p.ema9, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3 });
    const ema21 = chart.addSeries(LineSeries, { color: p.ema21, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3 });
    const ema20 = chart.addSeries(LineSeries, { color: p.ema20, lineWidth: 1, lineStyle: LineStyle.Dashed, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3, visible: false });
    const sma50 = chart.addSeries(LineSeries, { color: p.sma50, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerRadius: 3, visible: false });

    const watermark = createTextWatermark(chart.panes()[0], {
      horzAlign: "center",
      vertAlign: "center",
      lines: [{ text: `${coinFromSymbol(symbol)}USDT · ${TF_LABELS[interval]}`, color: p.watermark, fontSize: 34, fontStyle: "bold" }],
    });

    const bundle: ChartRefs = { chart, candle, volume, ema9, ema21, ema20, sma50, rsi: null, rsiLines: [], levelLines: [], watermark };
    if (showRsiRef.current) attachRsiPane(bundle, mode);
    refs.current = bundle;

    chart.subscribeCrosshairMove((param) => {
      const t = typeof param.time === "number" ? param.time : null;
      if (t === hoverTimeRef.current) return;
      hoverTimeRef.current = t;
      if (t === null || !dataRef.current) {
        setHoverBar(null);
      } else {
        setHoverBar(dataRef.current.byTime.get(t) ?? null);
      }
    });

    const fitSize = () => {
      const cw = el.clientWidth;
      const ch = el.clientHeight;
      if (cw > 0 && ch > 0) chart.resize(cw, ch);
    };
    const raf = window.requestAnimationFrame(fitSize);
    const ro = new ResizeObserver(fitSize);
    ro.observe(el);

    return () => {
      window.cancelAnimationFrame(raf);
      ro.disconnect();
      refs.current = null;
      chart.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- chart mounts once per modal open; symbol/interval/theme update via dedicated effects
  }, [attachRsiPane]);

  // Apply Dark / Light theme live without losing zoom/pan
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    const p = PALETTE[theme];
    r.chart.applyOptions(chartThemeOptions(theme));
    r.candle.applyOptions({
      upColor: p.up, downColor: p.down,
      borderUpColor: p.up, borderDownColor: p.down,
      wickUpColor: p.up, wickDownColor: p.down,
    });
    r.ema9.applyOptions({ color: p.ema9 });
    r.ema21.applyOptions({ color: p.ema21 });
    r.ema20.applyOptions({ color: p.ema20 });
    r.sma50.applyOptions({ color: p.sma50 });
    if (r.rsi) {
      r.rsi.applyOptions({ color: p.rsi });
      const colors = [p.down, p.rsiMid, p.up];
      r.rsiLines.forEach((l, i) => l.applyOptions({ color: colors[i] }));
      r.chart.priceScale("right", 1).applyOptions({ borderColor: p.border });
    }
    if (dataRef.current) {
      r.volume.setData(
        dataRef.current.candles.map((c) => ({
          time: c.time as UTCTimestamp,
          value: c.volume,
          color: c.close >= c.open ? p.volUp : p.volDown,
        })),
      );
    }
  }, [theme]);

  // Keep background watermark synced with symbol / timeframe / theme
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    r.watermark.applyOptions({
      lines: [{ text: `${coin}USDT · ${TF_LABELS[interval]}`, color: PALETTE[theme].watermark, fontSize: 34, fontStyle: "bold" }],
    });
  }, [theme, coin, interval]);

  // Toggle indicator overlays
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    r.ema9.applyOptions({ visible: showEma921 });
    r.ema21.applyOptions({ visible: showEma921 });
  }, [showEma921]);

  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    r.ema20.applyOptions({ visible: showEma2050 });
    r.sma50.applyOptions({ visible: showEma2050 });
  }, [showEma2050]);

  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    if (showRsi) attachRsiPane(r, themeRef.current);
    else detachRsiPane(r);
  }, [showRsi, attachRsiPane, detachRsiPane]);

  // Entry / SL / TP / Liq price lines (reactive to live 10s snapshot + theme + toggle)
  useEffect(() => {
    const r = refs.current;
    if (!r) return;
    for (const l of r.levelLines) r.candle.removePriceLine(l);
    r.levelLines = [];
    if (!showLevels) return;
    const p = PALETTE[theme];
    const add = (price: number | null, color: string, title: string, style: LineStyle, lineWidth: 1 | 2) => {
      if (price === null) return;
      r.levelLines.push(r.candle.createPriceLine({ price, color, lineWidth, lineStyle: style, axisLabelVisible: true, title }));
    };
    if (position) {
      add(extractEntryPrice(position.price), p.entry, `ENTRY ${position.sideCode === "LONG" ? "▲" : "▼"}`, LineStyle.Solid, 2);
      add(parseChartPrice(position.stopPx), p.sl, "SL", LineStyle.Dashed, 2);
      add(parseChartPrice(position.tpPx), p.tp, "TP", LineStyle.Dashed, 2);
      add(parseChartPrice(position.liq), p.liq, "LIQ", LineStyle.Dotted, 1);
    } else if (closedPosition) {
      add(parseChartPrice(closedPosition.entry), p.entry, `ENTRY (${closedPosition.sideCode})`, LineStyle.Dashed, 1);
    }
  }, [position, closedPosition, showLevels, theme]);

  // Fetch klines (resetZoom = true when changing symbol/timeframe, false on 10s silent live tick)
  const fetchKlines = useCallback(async (resetZoom: boolean) => {
    inflightRef.current?.abort();
    const ac = new AbortController();
    inflightRef.current = ac;
    if (resetZoom) {
      setLoading(true);
      setError(null);
      setHoverBar(null);
      hoverTimeRef.current = null;
    } else {
      setRefreshing(true);
    }
    try {
      const res = await fetch(`/api/klines?symbol=${encodeURIComponent(symbol)}&interval=${interval}&limit=240`, {
        cache: "no-store",
        signal: ac.signal,
      });
      if (res.status === 401) { window.location.assign("/login"); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as { candles?: Candle[] };
      const candles = Array.isArray(json.candles) ? json.candles : [];
      if (candles.length === 0) throw new Error("empty");

      const prevLen = dataRef.current?.candles.length ?? 0;
      const computed = buildComputed(candles);
      dataRef.current = computed;
      setLatest(computed.latest);
      setPrecision(computed.precision);
      setError(null);

      const r = refs.current;
      if (r) {
        const p = PALETTE[themeRef.current];
        const savedRange = resetZoom ? null : r.chart.timeScale().getVisibleLogicalRange();
        const priceFmt = { type: "price" as const, precision: computed.precision, minMove: computed.minMove };
        r.candle.applyOptions({ priceFormat: priceFmt });
        r.ema9.applyOptions({ priceFormat: priceFmt });
        r.ema21.applyOptions({ priceFormat: priceFmt });
        r.ema20.applyOptions({ priceFormat: priceFmt });
        r.sma50.applyOptions({ priceFormat: priceFmt });

        r.candle.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
        r.volume.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, value: c.volume, color: c.close >= c.open ? p.volUp : p.volDown })));
        r.ema9.setData(computed.ema9);
        r.ema21.setData(computed.ema21);
        r.ema20.setData(computed.ema20);
        r.sma50.setData(computed.sma50);
        if (r.rsi) r.rsi.setData(computed.rsi14);

        r.watermark.applyOptions({
          lines: [{ text: `${coinFromSymbol(symbol)}USDT · ${TF_LABELS[interval]}`, color: p.watermark, fontSize: 34, fontStyle: "bold" }],
        });

        if (resetZoom) {
          const n = candles.length;
          if (n > 95) r.chart.timeScale().setVisibleLogicalRange({ from: n - 95, to: n + 6 });
          else r.chart.timeScale().fitContent();
        } else if (savedRange) {
          // Keep user's exact zoom/pan; if a new candle opened while parked at the right edge, track it gently
          const delta = candles.length - prevLen;
          const atLiveEdge = prevLen > 0 && savedRange.to >= prevLen - 2;
          const shift = delta > 0 && atLiveEdge ? delta : 0;
          r.chart.timeScale().setVisibleLogicalRange({ from: savedRange.from + shift, to: savedRange.to + shift });
        }
      }
    } catch {
      if (!ac.signal.aborted && resetZoom) {
        setError(`ดึงแท่งเทียน ${symbol} (${TF_LABELS[interval]}) จาก Binance ไม่สำเร็จ`);
      }
    } finally {
      if (inflightRef.current === ac) {
        inflightRef.current = null;
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [symbol, interval]);

  // Load immediately on symbol/interval change + silent live 10s polling while open
  useEffect(() => {
    void fetchKlines(true);
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") void fetchKlines(false);
    }, REFRESH_MS);
    return () => {
      window.clearInterval(id);
      inflightRef.current?.abort();
    };
  }, [fetchKlines]);

  const activeBar = hoverBar ?? latest;
  const activeSide: SideCode | undefined = position?.sideCode ?? closedPosition?.sideCode ?? coins.find((c) => c.symbol === symbol)?.sideCode;
  const hasLevels = position !== null || closedPosition !== null;
  const pctStr = activeBar ? `${activeBar.pct >= 0 ? "+" : ""}${activeBar.pct.toFixed(2)}%` : "";
  const rsiCls = activeBar?.rsi14 !== null && activeBar?.rsi14 !== undefined
    ? activeBar.rsi14 >= 70 ? "neg" : activeBar.rsi14 <= 30 ? "pos" : ""
    : "";

  // Ensure the currently viewed symbol is always represented in the switcher strip
  const switcherCoins = useMemo(() => {
    if (coins.some((c) => c.symbol === symbol)) return coins;
    return [{ symbol, coin, group: "other" as const, sideCode: activeSide }, ...coins];
  }, [coins, symbol, coin, activeSide]);

  return (
    <div className="chart-shell">
      <header className="chart-head">
        <div className="chart-title-grp">
          {activeSide && <SideBadge code={activeSide} text={activeSide === "LONG" ? "▲ LONG" : "▼ SHORT"} />}
          <h2 id="chart-dlg-title" className="chart-sym">{coin}<span>/USDT</span></h2>
          {latest && (
            <span className="chart-px num">
              {formatChartPrice(latest.close, precision)}
              <small className={latest.pct >= 0 ? "pos" : "neg"}>
                {" "}{latest.pct >= 0 ? "+" : ""}{latest.pct.toFixed(2)}%
              </small>
            </span>
          )}
          <span className="chart-live-tag" title="อัปเดตแท่งเทียนสดจาก Binance Futures ทุก 10 วินาที">
            <span className="beacon" aria-hidden="true" />สด 10 วิ
          </span>
        </div>

        <div className="chart-tf-grp" role="group" aria-label="เลือก Timeframe">
          {KLINE_INTERVALS.map((tf) => (
            <button
              key={tf}
              type="button"
              className={`chart-tf${interval === tf ? " is-on" : ""}`}
              aria-pressed={interval === tf}
              onClick={() => setIntervalTf(tf)}
            >
              {TF_LABELS[tf]}
            </button>
          ))}
        </div>

        <div className="chart-head-acts">
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => void fetchKlines(false)}
            disabled={loading}
            title="ดึงแท่งเทียนล่าสุดจาก Binance ทันที (ไม่รีเซ็ตการซูม)"
          >
            <span className={refreshing ? "spin-once" : undefined} aria-hidden="true">↻</span> รีเฟรช
          </button>
          <button
            type="button"
            className="btn btn-sm btn-x"
            onClick={onClose}
            aria-label="ปิดหน้าต่างกราฟ"
            title="ปิดหน้าต่างกราฟ (Esc)"
          >
            ✕
          </button>
        </div>
      </header>

      <div className="chart-coins" role="navigation" aria-label="สลับเหรียญดูกราฟ">
        {switcherCoins.map((c) => {
          const active = c.symbol === symbol;
          return (
            <button
              key={c.symbol}
              type="button"
              className={`coin-pill${active ? " is-on" : ""}${c.group === "btc" ? " is-btc" : ""}`}
              aria-pressed={active}
              onClick={() => onSelectSymbol(c.symbol)}
              title={c.group === "open" ? `Position เปิดอยู่ · ${c.coin}` : c.group === "btc" ? "ดัชนี BTCUSDT" : `แผนเทรด · ${c.coin}`}
            >
              {c.sideCode && <span className={`pill-dot ${c.sideCode === "LONG" ? "dot-long" : "dot-short"}`} aria-hidden="true" />}
              <span>{c.coin}</span>
            </button>
          );
        })}
      </div>

      <div className="chart-bar">
        <div className="chart-toggles" role="group" aria-label="เปิด/ปิดอินดิเคเตอร์">
          <button type="button" className={`ind-pill${showEma921 ? " is-on" : ""}`} aria-pressed={showEma921} onClick={() => setShowEma921((v) => !v)}>
            <span className="ind-sw sw-ema9" aria-hidden="true" /><span className="ind-sw sw-ema21" aria-hidden="true" />EMA 9/21
          </button>
          <button type="button" className={`ind-pill${showEma2050 ? " is-on" : ""}`} aria-pressed={showEma2050} onClick={() => setShowEma2050((v) => !v)}>
            <span className="ind-sw sw-ema20" aria-hidden="true" /><span className="ind-sw sw-sma50" aria-hidden="true" />EMA 20 / SMA 50
          </button>
          <button type="button" className={`ind-pill${showRsi ? " is-on" : ""}`} aria-pressed={showRsi} onClick={() => setShowRsi((v) => !v)}>
            <span className="ind-sw sw-rsi" aria-hidden="true" />RSI 14
          </button>
          {hasLevels && (
            <button type="button" className={`ind-pill${showLevels ? " is-on" : ""}`} aria-pressed={showLevels} onClick={() => setShowLevels((v) => !v)}>
              <span className="ind-sw sw-lvl" aria-hidden="true" />Entry / SL / TP / Liq
            </button>
          )}
        </div>
        <span className="chart-hint">ลากเลื่อนดูอดีต · เลื่อนลูกกลิ้งซูม · เวลาไทย (UTC+7)</span>
      </div>

      {position && (
        <div className="chart-pos-strip" aria-label="ข้อมูล Position ปัจจุบัน">
          <span><b>เข้า → Mark</b> <i className="mono">{position.price}</i></span>
          <span><b>SL</b> <i className="mono neg">{position.stopPx}</i></span>
          <span><b>TP</b> <i className="mono pos">{position.tpPx}</i></span>
          <span><b>Liq</b> <i className="mono">{position.liq}</i></span>
          <span><b>PnL</b> <i className={`mono ${pnlClass(position.pnlSign)}`}>{position.pnl}</i></span>
          <span><b>Stage · MFE</b> <i className="mono">{position.stage}{position.mfePct !== "-" ? ` ${position.mfePct}` : ""}</i></span>
        </div>
      )}

      <div className="chart-stage-wrap">
        {activeBar && (
          <div className="chart-hud" aria-live="off">
            <div className="hud-row">
              <span className="hud-t">{bkkCrosshairTime(activeBar.time)}</span>
              <span>O <b className="mono">{formatChartPrice(activeBar.open, precision)}</b></span>
              <span>H <b className="mono">{formatChartPrice(activeBar.high, precision)}</b></span>
              <span>L <b className="mono">{formatChartPrice(activeBar.low, precision)}</b></span>
              <span>C <b className={`mono ${activeBar.pct >= 0 ? "pos" : "neg"}`}>{formatChartPrice(activeBar.close, precision)}</b></span>
              <span className={`mono ${activeBar.pct >= 0 ? "pos" : "neg"}`}>{pctStr}</span>
              <span>Vol <b className="mono">{formatVolume(activeBar.volume)}</b></span>
            </div>
            <div className="hud-row hud-inds">
              {showEma921 && (
                <>
                  <span className="hud-ema9">EMA9 <b>{formatChartPrice(activeBar.ema9, precision)}</b></span>
                  <span className="hud-ema21">EMA21 <b>{formatChartPrice(activeBar.ema21, precision)}</b></span>
                </>
              )}
              {showEma2050 && (
                <>
                  <span className="hud-ema20">EMA20 <b>{formatChartPrice(activeBar.ema20, precision)}</b></span>
                  <span className="hud-sma50">SMA50 <b>{formatChartPrice(activeBar.sma50, precision)}</b></span>
                </>
              )}
              {showRsi && (
                <span className={`hud-rsi ${rsiCls}`}>
                  RSI(14) <b>{activeBar.rsi14 !== null ? activeBar.rsi14.toFixed(1) : "-"}</b>
                </span>
              )}
            </div>
          </div>
        )}

        <div ref={stageRef} className="chart-stage" />

        {loading && (
          <div className="chart-overlay" role="status">
            <span className="spin" aria-hidden="true" /> กำลังโหลดกราฟ {coin}/USDT ({TF_LABELS[interval]})…
          </div>
        )}
        {!loading && error && (
          <div className="chart-overlay is-err" role="alert">
            <span>{error}</span>
            <button type="button" className="btn btn-sm btn-gold" onClick={() => void fetchKlines(true)}>ลองใหม่</button>
          </div>
        )}
      </div>
    </div>
  );
}
