/* Airport FIDS terminal-board furniture: plane glyph and station emblem (inline SVG, no packages). */

/** Outline airliner (Material "flight" path, Apache-2.0) in a framed tile; a green live dot when Fast-Guard is up. */
export function PlaneGlyph({ live }: { live: boolean }) {
  return (
    <span className={`glyph${live ? " live" : ""}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="24" height="24" focusable="false">
        <path className="plane" d="M21 16v-2l-8-5V3.5c0-.83-.67-1.5-1.5-1.5S10 2.67 10 3.5V9l-8 5v2l8-2.5V19l-2 1.5V22l3.5-1 3.5 1v-1.5L13 19v-5.5l8 2.5z" />
      </svg>
    </span>
  );
}

/** "BKK · V23" station badge with an orbit swoosh, in the spirit of the airport code badge on a FIDS header. */
export function StationEmblem() {
  return (
    <span className="emblem" role="img" aria-label="สถานี BKK · ระบบเทรด V23" title="สถานี BKK (เวลาไทย) · ระบบเทรด V23">
      <svg viewBox="0 0 78 32" width="78" height="32" focusable="false" aria-hidden="true">
        <path className="orbit" d="M5 23 C 19 5, 59 1, 74 11" />
        <text className="emb-code" x="38" y="24" textAnchor="middle">BKK</text>
        <text className="emb-sub" x="76" y="31" textAnchor="end">V23</text>
      </svg>
    </span>
  );
}
