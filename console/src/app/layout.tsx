import type { Metadata, Viewport } from "next";
import { Barlow_Condensed, JetBrains_Mono, Noto_Sans_Thai } from "next/font/google";
import type { ReactNode } from "react";
import "./globals.css";

// Downloaded once at build time and self-hosted from /_next/static (no runtime call to Google). globals.css falls back to system fonts.
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono", display: "swap" });                       // digits / split-flap slots: equal pitch
const display = Barlow_Condensed({ subsets: ["latin"], weight: ["500", "600", "700"], variable: "--font-display", display: "swap" }); // FIDS headers
const thai = Noto_Sans_Thai({ subsets: ["thai", "latin"], variable: "--font-thai", display: "swap" });                // Thai UI text

export const metadata: Metadata = {
  title: "BotTrade V23 — หน้าจอ Monitor พอร์ตและ Position",
  description: "หน้าจอ Monitor พอร์ตและปิด Position สำหรับระบบเทรด V23 Binance Futures",
  robots: { index: false, follow: false },   // private monitor: never in a search index
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#08152b" };

// Runs before first paint (zero FOUC): saved choice -> OS preference -> dark. Keep in sync with ThemeToggle (key "bt_theme").
const THEME_BOOT = `(function(){var d=document.documentElement,t="dark";try{var s=localStorage.getItem("bt_theme");t=s==="light"||s==="dark"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: light)").matches?"light":"dark")}catch(e){}d.dataset.theme=t;d.style.colorScheme=t;if(t==="light"){var m=document.querySelectorAll('meta[name="theme-color"]');for(var i=0;i<m.length;i++)m[i].setAttribute("content","#0047ab")}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="th" data-theme="dark" className={`${mono.variable} ${display.variable} ${thai.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
