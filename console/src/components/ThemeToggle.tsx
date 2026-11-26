"use client";

export const THEME_KEY = "bt_theme";
const THEME_COLOR = { dark: "#08152b", light: "#0047ab" } as const;   // FIDS banner colours (layout.tsx boot script uses the same)

/** Dark / light switch. The visible state is pure CSS ([data-theme] set before first paint in layout.tsx), so SSR and hydration never disagree. */
export function ThemeToggle() {
  function toggle() {
    const root = document.documentElement;
    const next = root.dataset.theme === "light" ? "dark" : "light";
    root.classList.add("theme-anim");
    root.dataset.theme = next;
    root.style.colorScheme = next;
    document.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", THEME_COLOR[next]));
    try { window.localStorage.setItem(THEME_KEY, next); } catch { /* private mode: theme still switches for this page */ }
    window.setTimeout(() => root.classList.remove("theme-anim"), 650);
  }
  return (
    <button className="btn btn-sm theme-toggle" type="button" onClick={toggle} title="กดเพื่อสลับโหมด มืด / สว่าง (จำค่าไว้ในเครื่องนี้)">
      <span className="tt-track" aria-hidden="true">
        <span className="tt-knob">
          <span className="tt-ico tt-moon">🌙</span>
          <span className="tt-ico tt-sun">☀️</span>
        </span>
      </span>
      <span className="tt-lbl tt-dark">มืด</span>
      <span className="tt-lbl tt-light">สว่าง</span>
    </button>
  );
}
