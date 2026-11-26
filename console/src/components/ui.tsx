"use client";
import { useContext, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { newRowIds } from "@/lib/motion";
import type { PnlSign, SideCode } from "@/lib/types";
import { FlapReady, SplitFlap } from "./motion";
import { ColumnResizeHandle, ResetColumnWidthsButton, useResizableColumns, type ResizableColumnDef } from "./ResizableTable";

/* Shared building blocks: every tab uses these, so tables / tabs / badges / dialogs look and behave identically. */

const ROW_LAND_MS = 4_500;   // keep tr.row-new until the slowest cell (stagger + 3.2s tint) has finished

export const pnlClass = (s: PnlSign) => (s > 0 ? "pos" : s < 0 ? "neg" : "");

export function SideBadge({ code, text }: { code: SideCode; text: string }) {
  return <span className={`badge ${code === "LONG" ? "side-long" : "side-short"}`}>{text}</span>;
}

export interface TabDef<K extends string> { key: K; label: string }

/** WAI-ARIA tablist: arrow keys move the selection, only the selected tab is in the tab order. */
export function Tabs<K extends string>({ tabs, value, onChange, idBase, label, seg }: {
  tabs: readonly TabDef<K>[]; value: K; onChange: (k: K) => void; idBase: string; label: string; seg?: boolean;
}) {
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = tabs.findIndex((t) => t.key === value);
    const next = e.key === "ArrowRight" ? i + 1 : e.key === "ArrowLeft" ? i - 1 : e.key === "Home" ? 0 : e.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    e.preventDefault();
    const t = tabs[(next + tabs.length) % tabs.length];
    onChange(t.key);
    document.getElementById(`${idBase}-tab-${t.key}`)?.focus();
  }
  return (
    <div className={seg ? "seg" : "tabs"} role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button key={t.key} id={`${idBase}-tab-${t.key}`} className="tab" type="button" role="tab" aria-selected={t.key === value}
          aria-controls={`${idBase}-panel`} tabIndex={t.key === value ? 0 : -1} onClick={() => onChange(t.key)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function Panel({ idBase, k, children }: { idBase: string; k: string; children: ReactNode }) {
  return <div className="panel" id={`${idBase}-panel`} role="tabpanel" aria-labelledby={`${idBase}-tab-${k}`} tabIndex={-1}>{children}</div>;
}

export interface Col<T> {
  key?: string;
  head: string;
  width?: number;
  minPx?: number;
  align?: "left" | "right" | "center";
  sortValue?: (r: T) => string | number;
  cell: (r: T) => ReactNode;
  title?: (r: T) => string | undefined;
  wrap?: boolean;
  strong?: boolean;
}

function cmpVal(a: string | number, b: string | number): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  const na = typeof a === "number" ? a : Number(String(a).replace(/[,+%$USDT\s]/g, ""));
  const nb = typeof b === "number" ? b : Number(String(b).replace(/[,+%$USDT\s]/g, ""));
  if (Number.isFinite(na) && Number.isFinite(nb) && String(a).trim() !== "" && String(b).trim() !== "") return na - nb;
  return String(a).localeCompare(String(b), "th", { numeric: true });
}

export function DataTable<T>({
  storageKey,
  caption,
  cols,
  rows,
  empty,
  rowKey,
  selected,
  onSelect,
  onHeaderRefresh,
}: {
  storageKey?: string;
  caption: string;
  cols: readonly Col<T>[];
  rows: readonly T[];
  empty: string;
  rowKey: (r: T, i: number) => string;
  selected?: string | null;
  onSelect?: (key: string) => void;
  onHeaderRefresh?: () => void;
}) {
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const colDefs: ResizableColumnDef[] = useMemo(
    () => cols.map((c, idx) => ({
      key: c.key ?? `c${idx}_${c.head}`,
      defaultWidth: c.width ?? (c.wrap ? 22 : 10),
      minPx: c.minPx ?? 40,
    })),
    [cols],
  );

  const {
    containerRef,
    isCustomized,
    activeKey,
    tableStyle,
    getColStyle,
    startResize,
    resetColumnWidth,
    resetWidths,
    handleHeaderClick,
  } = useResizableColumns({
    storageKey: storageKey ?? caption,
    columns: colDefs,
    onHeaderRefresh: (colKey) => {
      if (sortCol === colKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      else { setSortCol(colKey); setSortDir("desc"); }
      onHeaderRefresh?.();
    },
  });

  const displayRows = useMemo(() => {
    if (!sortCol) return rows;
    const idx = colDefs.findIndex((c) => c.key === sortCol);
    if (idx < 0) return rows;
    const col = cols[idx];
    const getVal = col.sortValue ?? ((r: T) => {
      const v = col.title?.(r);
      if (v !== undefined) return v;
      const cell = col.cell(r);
      return typeof cell === "string" || typeof cell === "number" ? cell : "";
    });
    return [...rows].sort((a, b) => {
      const d = cmpVal(getVal(a), getVal(b));
      return sortDir === "asc" ? d : -d;
    });
  }, [rows, sortCol, sortDir, colDefs, cols]);

  // brand-new rows (new position / closed trade / 15m round) cascade-flip in like a new flight on the departure board.
  // rowKey(r, 0) = the row identity without its list index, so a row inserted on top does not make every row "new".
  const flapReady = useContext(FlapReady);
  const seenIds = useRef<Set<string> | null>(null);
  const freshUntil = useRef(new Map<string, number>());   // id -> ms until which the row keeps its landing animation class
  useMemo(() => {
    const ids = rows.map((r) => rowKey(r, 0));
    const now = Date.now();
    for (const [id, until] of freshUntil.current) if (until <= now) freshUntil.current.delete(id);
    if (flapReady) for (const id of newRowIds(seenIds.current, ids)) freshUntil.current.set(id, now + ROW_LAND_MS);
    seenIds.current = new Set(ids);
  }, [rows, flapReady]);   // eslint-disable-line react-hooks/exhaustive-deps -- rowKey is an inline lambda; rows drive the diff
  const renderNow = Date.now();

  if (rows.length === 0) return <p className="empty">{empty}</p>;

  return (
    <div className="table-box">
      <div className="table-toolbar">
        <span className="table-meta">{caption} · {rows.length} รายการ (คลิกหัวคอลัมน์เพื่อเรียง+รีเฟรช · ลากขอบเพื่อปรับขนาด)</span>
        <ResetColumnWidthsButton isCustomized={isCustomized} onReset={resetWidths} testId={`reset-${storageKey ?? "tbl"}`} />
      </div>
      <div ref={containerRef} className="table-wrap" tabIndex={0} role="region" aria-label={caption}>
        <table data-resizable-managed="true" style={tableStyle} className="table">
          <caption className="sr-only">{caption}</caption>
          <colgroup>
            {colDefs.map((cd) => <col key={cd.key} style={getColStyle(cd.key)} />)}
          </colgroup>
          <thead>
            <tr>
              {cols.map((c, idx) => {
                const ck = colDefs[idx].key;
                const isSorted = sortCol === ck;
                const alignCls = c.align === "right" ? "ta-r" : c.align === "center" ? "ta-c" : "";
                return (
                  <th
                    key={ck}
                    data-col-key={ck}
                    scope="col"
                    style={getColStyle(ck)}
                    className={`th-res ${alignCls}`}
                    onClick={() => handleHeaderClick(ck)}
                    title={`${c.head} — คลิกเพื่อเรียงลำดับและดึงข้อมูลล่าสุด`}
                  >
                    <span className="th-label">
                      <span className="th-text">{c.head}</span>
                      {isSorted && <span className="sort-ind" aria-hidden="true">{sortDir === "asc" ? "▲" : "▼"}</span>}
                    </span>
                    <ColumnResizeHandle
                      colKey={ck}
                      isActive={activeKey === ck}
                      onStartResize={startResize}
                      onResetColumn={resetColumnWidth}
                    />
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {displayRows.map((r, i) => {
              const k = rowKey(r, i);
              const pick = onSelect ? {
                tabIndex: 0,
                "aria-selected": selected === k,
                onClick: () => onSelect(k),
                onKeyDown: (e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(k); } },
              } : {};
              const isNew = (freshUntil.current.get(rowKey(r, 0)) ?? 0) > renderNow;
              return (
                <tr key={k} className={isNew ? "row-new" : undefined} {...pick}>
                  {cols.map((c, idx) => {
                    const ck = colDefs[idx].key;
                    const alignCls = c.align === "right" ? "ta-r" : c.align === "center" ? "ta-c" : "";
                    const cellContent = c.cell(r);
                    const tip = c.title?.(r) ?? (typeof cellContent === "string" || typeof cellContent === "number" ? String(cellContent) : undefined);
                    return (
                      <td
                        key={ck}
                        style={isNew ? { ...getColStyle(ck), "--c": idx } as CSSProperties : getColStyle(ck)}
                        className={`${c.wrap ? "wrap" : "trunc"} ${c.strong ? "strong" : ""} ${alignCls}`}
                        title={tip}
                      >
                        {typeof cellContent === "string" || typeof cellContent === "number" ? <SplitFlap value={String(cellContent)} /> : cellContent}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Native <dialog>: focus trap, Esc and the backdrop come from the browser. */
export function Modal({ open, onClose, title, children, foot }: { open: boolean; onClose: () => void; title: string; children: ReactNode; foot?: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} aria-labelledby="dlg-title" onClose={onClose} onClick={(e) => { if (e.target === ref.current) onClose(); }}>
      {open && (
        <>
          <div className="dlg-head">
            <h2 id="dlg-title">{title}</h2>
            <button className="btn btn-x" type="button" aria-label="ปิดหน้าต่าง" onClick={onClose}>✕</button>
          </div>
          <div className="dlg-body">{children}</div>
          {foot && <div className="dlg-foot">{foot}</div>}
        </>
      )}
    </dialog>
  );
}
