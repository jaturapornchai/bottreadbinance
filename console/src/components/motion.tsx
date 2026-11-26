"use client";
import { createContext, useContext, useId, useState, type CSSProperties } from "react";
import { cbUsedPermille, decDir, flapTokens, gateSlots, graphemes, leadNumber, type FlapToken, type Polarity } from "@/lib/motion";

/* Live motion graphics: pure CSS keyframes + inline SVG (no packages). prefers-reduced-motion stops them in globals.css. */

/** false until the first snapshot has rendered: values arriving on page load settle silently, only later updates flip. */
export const FlapReady = createContext(true);

const FLAP_STAGGER_MS = 95;     // unhurried left-to-right cascade so the eye can follow each digit
const FLAP_STAGGER_MAX_MS = 1_330; // cap (15 flaps) so long values still settle well before the next 10s refresh
const FLAP_3D_MAX = 16;   // longer words get a flat tint flip so a nowrap cell keeps its ellipsis

type Dir = -1 | 0 | 1;

function Overlays({ old, text }: { old: string; text: string }) {
  return (
    <>
      <span className="fl fl-stay" aria-hidden="true">{old}</span>
      <span className="fl fl-out" aria-hidden="true">{old}</span>
      <span className="fl fl-in" aria-hidden="true">{text}</span>
    </>
  );
}

/**
 * Airport split-flap (Solari board): when `value` changes after first load, only the changed characters / words flip,
 * left to right; unchanged text stays still. Numbers are always monospace (idle too) and each flipping digit is an
 * exact 1ch slot with its tile drawn inside the slot, so tiles never overlap and idle <-> flipping never shifts width.
 * Each flap is two halves on a centre hinge: up = พับขึ้น (emerald), down = พับลง (crimson), text / new = gold drop.
 */
export function SplitFlap({ value }: { value: string }) {
  const ready = useContext(FlapReady);
  const [st, setSt] = useState<{ value: string; toks: FlapToken[]; dir: Dir; v: number }>(() => ({ value, toks: flapTokens(value, value), dir: 0, v: 0 }));
  let cur = st;
  if (st.value !== value) {
    // adjust state while rendering (React's documented pattern) so the very first paint of the new value already flips
    cur = {
      value,
      toks: flapTokens(ready ? st.value : value, value),
      dir: ready ? decDir(leadNumber(st.value), leadNumber(value)) : 0,
      v: st.v + 1,
    };
    setSt(cur);
  }
  let k = 0;
  const delay = () => ({ "--d": `${Math.min(k++ * FLAP_STAGGER_MS, FLAP_STAGGER_MAX_MS)}ms` } as CSSProperties);
  return (
    <span key={cur.v} className={`sf sf-${cur.dir > 0 ? "up" : cur.dir < 0 ? "down" : "new"}`}>
      {cur.toks.map((t, i) => {
        if (t.kind === "space") return t.text;
        if (t.kind === "num") {
          return (
            <span key={i} className="sf-num">
              {t.chars.map((c, j) => (c.flip ? (
                <span key={j} className="flap sf-ch" style={delay()}>{c.text}<Overlays old={c.old} text={c.text} /></span>
              ) : c.text))}
            </span>
          );
        }
        if (!t.flip) return t.text;
        const flat = graphemes(t.text).length > FLAP_3D_MAX;
        return (
          <span key={i} className={flat ? "flap flap-word flap-flat" : "flap flap-word"} style={delay()}>
            {t.text}
            {!flat && <Overlays old={t.old} text={t.text} />}
          </span>
        );
      })}
    </span>
  );
}

/** Airport gate slots: LONG (green) / SHORT (red) / free; capped = MAX_OPEN gates, unlimited = in use + affordable by free margin. */
export function SlotBar({ longN, shortN, maxOpen, afford }: { longN: number; shortN: number; maxOpen: number | null; afford: number | null }) {
  const gates = gateSlots(longN, shortN, maxOpen, afford);
  const used = longN + shortN;
  const label = maxOpen === null
    ? `ถือ ${used} ไม้ (LONG ${longN} · SHORT ${shortN}) · เปิดเพิ่มได้ ${afford ?? "-"} ไม้ตามเงินว่าง`
    : `ใช้ ${used} จาก ${Math.max(maxOpen, used)} ช่อง · LONG ${longN} · SHORT ${shortN}`;
  return (
    <span className="kpi-viz slotbar gates" role="img" aria-label={label} title={label}>
      {gates.map((g, i) => <i key={i} className={`slot slot-${g.state}`} style={{ "--i": i } as CSSProperties}>{g.label}</i>)}
    </span>
  );
}

/** Circuit Breaker cushion: track = remaining cushion, fill = drawdown used vs the live limits (whichever is closer). */
export function CbGauge({ ddUsdt, ddPct, tripped, limitUsdt, limitPct }: { ddUsdt: string; ddPct: string; tripped: boolean; limitUsdt: string; limitPct: string }) {
  const used = tripped ? 1000 : cbUsedPermille(ddUsdt, ddPct, limitUsdt, limitPct);
  const level = tripped || used >= 800 ? "danger" : used >= 500 ? "warn" : "safe";
  const usedTxt = `${Math.floor(used / 10)}.${used % 10}%`;
  return (
    <span
      className={`kpi-viz gauge gauge-${level}`}
      role="meter"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={used / 10}
      aria-label={`ใช้ไป ${usedTxt} ของเกณฑ์ตัด Circuit Breaker`}
      title={`ใช้ไป ${usedTxt} ของเกณฑ์ตัด -${limitUsdt} USDT / -${limitPct}% · เหลือกันชน ${Math.floor((1000 - used) / 10)}%`}
      style={{ "--p": `${used / 10}%` } as CSSProperties}
    >
      <i className="gauge-fill" />
    </span>
  );
}

function wavePath(amp: number, period = 40, width = 320, mid = 15): string {
  let d = `M0 ${mid}`;
  for (let x = 0; x < width; x += period) {
    d += ` Q${x + period / 4} ${mid - amp} ${x + period / 2} ${mid} Q${x + (period * 3) / 4} ${mid + amp} ${x + period} ${mid}`;
  }
  return d;
}
const AMP: Record<Polarity, number> = { bull: 6, bear: 6, squeeze: 9, flat: 3 };

/** Subtle drifting market-pulse wave behind the Regime card, coloured and tilted by bullish / bearish / squeeze polarity. */
export function RegimeWave({ polarity }: { polarity: Polarity }) {
  const gid = useId().replace(/:/g, "");
  const line = wavePath(AMP[polarity]);
  return (
    <svg className={`wave wave-${polarity}`} viewBox="0 0 160 30" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={`wg${gid}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity=".28" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <g className="wave-track">
        <path d={`${line} L320 30 L0 30 Z`} fill={`url(#wg${gid})`} />
        <path d={line} fill="none" stroke="currentColor" strokeWidth="1.4" vectorEffect="non-scaling-stroke" />
      </g>
    </svg>
  );
}
