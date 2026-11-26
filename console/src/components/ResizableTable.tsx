"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent } from "react";
import { normalizePercentages, redistributeColumnPercentages } from "@/lib/colwidths";

export { normalizePercentages, redistributeColumnPercentages };

export interface ResizableColumnDef {
  key: string;
  defaultWidth: number;
  minPx?: number;
}

export interface UseResizableColumnsOptions {
  storageKey: string;
  columns: readonly ResizableColumnDef[];
  onHeaderRefresh?: (colKey: string) => void;
}

const STORAGE_PREFIX = "sml_col_pct_v2_";
const DEFAULT_MIN_PX = 40;

export function useResizableColumns({ storageKey, columns, onHeaderRefresh }: UseResizableColumnsOptions) {
  const defaultPcts = useMemo(() => normalizePercentages(columns.map((c) => c.defaultWidth)), [columns]);
  const colKeys = useMemo(() => columns.map((c) => c.key), [columns]);
  const colSig = colKeys.join("|");

  const [pcts, setPcts] = useState<number[]>(defaultPcts);
  const [isCustomized, setIsCustomized] = useState(false);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [containerPx, setContainerPx] = useState(1100);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const pctsRef = useRef(pcts);
  pctsRef.current = pcts;
  const suppressClickRef = useRef(false);
  const refreshRef = useRef(onHeaderRefresh);
  refreshRef.current = onHeaderRefresh;

  useEffect(() => {
    let loaded: number[] | null = null;
    try {
      const raw = window.localStorage.getItem(STORAGE_PREFIX + storageKey);
      if (raw) {
        const parsed = JSON.parse(raw) as { keys?: string[]; pcts?: number[] };
        if (
          Array.isArray(parsed.keys)
          && Array.isArray(parsed.pcts)
          && parsed.keys.length === columns.length
          && parsed.pcts.length === columns.length
          && parsed.keys.every((k, i) => k === columns[i].key)
          && parsed.pcts.every((v) => typeof v === "number" && Number.isFinite(v) && v > 0)
        ) {
          loaded = normalizePercentages(parsed.pcts);
        }
      }
    } catch { /* ignore storage errors */ }
    if (loaded) {
      setPcts(loaded);
      setIsCustomized(true);
    } else {
      setPcts(defaultPcts);
      setIsCustomized(false);
    }
  }, [storageKey, colSig, columns, defaultPcts]);

  useEffect(() => {
    const el = containerRef.current;
    const measure = () => {
      const w = el?.clientWidth || window.innerWidth || 1100;
      if (w > 0) setContainerPx(w);
    };
    measure();
    let ro: ResizeObserver | null = null;
    if (el && typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(measure);
      ro.observe(el);
    }
    window.addEventListener("resize", measure);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const minPcts = useMemo(() => {
    const safeW = Math.max(320, containerPx);
    const raw = columns.map((c) => Math.max(3, ((c.minPx ?? DEFAULT_MIN_PX) / safeW) * 100));
    const sum = raw.reduce((a, b) => a + b, 0);
    return sum > 85 ? raw.map((v) => (v / sum) * 85) : raw;
  }, [columns, containerPx]);

  const persist = useCallback((next: number[]) => {
    try {
      window.localStorage.setItem(STORAGE_PREFIX + storageKey, JSON.stringify({ keys: colKeys, pcts: next }));
    } catch { /* ignore */ }
  }, [storageKey, colKeys]);

  const resetWidths = useCallback(() => {
    setPcts(defaultPcts);
    setIsCustomized(false);
    try { window.localStorage.removeItem(STORAGE_PREFIX + storageKey); } catch { /* ignore */ }
  }, [defaultPcts, storageKey]);

  const resetColumnWidth = useCallback((_colKey: string) => {
    resetWidths();
  }, [resetWidths]);

  const startResize = useCallback((colKey: string, e: MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const idx = colKeys.indexOf(colKey);
    if (idx < 0) return;
    const startX = e.clientX;
    const startPcts = [...pctsRef.current];
    const tableW = Math.max(320, containerRef.current?.clientWidth || containerPx);
    let moved = false;
    setActiveKey(colKey);

    const onMove = (ev: globalThis.MouseEvent) => {
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > 1) moved = true;
      const deltaPct = (dx / tableW) * 100;
      const updated = redistributeColumnPercentages(startPcts, idx, deltaPct, minPcts);
      setPcts(updated);
      setIsCustomized(true);
    };

    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      setActiveKey(null);
      if (moved) {
        persist(pctsRef.current);
        suppressClickRef.current = true;
        const cancelClick = (cev: globalThis.MouseEvent) => {
          cev.preventDefault();
          cev.stopPropagation();
          suppressClickRef.current = false;
        };
        window.addEventListener("click", cancelClick, { capture: true, once: true });
        setTimeout(() => {
          suppressClickRef.current = false;
          window.removeEventListener("click", cancelClick, { capture: true });
        }, 120);
      }
    };

    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [colKeys, containerPx, minPcts, persist]);

  const handleHeaderClick = useCallback((colKey: string) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    refreshRef.current?.(colKey);
  }, []);

  const tableStyle: CSSProperties = useMemo(() => ({ width: "100%", tableLayout: "fixed" }), []);

  const getColStyle = useCallback((colKey: string): CSSProperties => {
    const idx = colKeys.indexOf(colKey);
    const w = idx >= 0 && pcts[idx] !== undefined ? pcts[idx] : (100 / Math.max(1, columns.length));
    return { width: `${w.toFixed(2)}%` };
  }, [colKeys, pcts, columns.length]);

  return {
    containerRef,
    isCustomized,
    activeKey,
    tableStyle,
    getColStyle,
    startResize,
    resetColumnWidth,
    resetWidths,
    handleHeaderClick,
  };
}

export function ColumnResizeHandle({
  colKey,
  isActive,
  onStartResize,
  onResetColumn,
}: {
  colKey: string;
  isActive: boolean;
  onStartResize: (colKey: string, e: MouseEvent) => void;
  onResetColumn: (colKey: string) => void;
}) {
  return (
    <span
      role="separator"
      aria-orientation="vertical"
      aria-label={`ปรับความกว้างคอลัมน์ ${colKey}`}
      title="ลากเพื่อปรับขนาด · ดับเบิลคลิกคืนค่าเริ่มต้น"
      className={`col-resizer${isActive ? " is-active" : ""}`}
      onMouseDown={(e) => onStartResize(colKey, e)}
      onClick={(e) => e.stopPropagation()}
      onDoubleClick={(e) => {
        e.stopPropagation();
        onResetColumn(colKey);
      }}
    />
  );
}

export function ResetColumnWidthsButton({
  isCustomized,
  onReset,
  testId,
}: {
  isCustomized: boolean;
  onReset: () => void;
  testId?: string;
}) {
  return (
    <button
      type="button"
      className="btn btn-sm"
      disabled={!isCustomized}
      onClick={onReset}
      data-testid={testId}
      title="คืนความกว้างคอลัมน์ทั้งหมดกลับเป็นสัดส่วน 100% ค่าเริ่มต้น"
    >
      ↺ ค่าเริ่มต้น
    </button>
  );
}
