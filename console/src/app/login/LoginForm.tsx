"use client";
import { useState, type FormEvent } from "react";
import { PlaneGlyph, StationEmblem } from "@/components/fids";
import { ThemeToggle } from "@/components/ThemeToggle";

interface LoginReply { error?: string; left?: number; lockedS?: number; retryAfterS?: number }

const minutes = (s: number) => Math.max(1, Math.ceil(s / 60));

function message(status: number, r: LoginReply): string {
  if (status === 503) return "ระบบยังไม่ได้ตั้งรหัสผ่าน (CONSOLE_PASSWORD / CONSOLE_SECRET) — แจ้งผู้ดูแลเซิร์ฟเวอร์";
  if (status === 429) return `ใส่รหัสผิดหลายครั้ง ถูกล็อกชั่วคราว — ลองใหม่อีก ${minutes(r.retryAfterS ?? 300)} นาที`;
  if (status === 401) return (r.lockedS ?? 0) > 0 ? `รหัสผ่านไม่ถูกต้อง และถูกล็อกชั่วคราว ${minutes(r.lockedS ?? 300)} นาที` : `รหัสผ่านไม่ถูกต้อง (เหลืออีก ${r.left ?? 0} ครั้งก่อนถูกล็อก)`;
  return "เข้าสู่ระบบไม่สำเร็จ ลองใหม่อีกครั้ง";
}

export function LoginForm() {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy || password.length === 0) return;
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
      if (res.ok) {
        window.location.assign("/");
        return;
      }
      setError(message(res.status, (await res.json().catch(() => ({}))) as LoginReply));
      setPassword("");
    } catch {
      setError("เชื่อมต่อเซิร์ฟเวอร์ไม่ได้ ลองใหม่อีกครั้ง");
    }
    setBusy(false);
  }

  return (
    <>
      <div className="login-theme"><ThemeToggle /></div>
      <form className="login-card" onSubmit={submit} aria-labelledby="login-title">
        <div className="login-head">
          <PlaneGlyph live={false} />
          <div className="login-title">
            <h1 id="login-title">BOTTRADE TERMINAL</h1>
            <p className="sub">ประตูเข้าสู่ระบบ · Gate Access</p>
          </div>
          <StationEmblem />
        </div>
        <div className="login-strip"><span>Monitor พอร์ตและปิด Position</span><span>Binance Futures</span></div>
        <div className="login-body">
        <div className="field">
          <label htmlFor="pw">รหัสผ่าน</label>
          <input id="pw" className="input" type="password" autoComplete="current-password" required maxLength={256} autoFocus value={password} onChange={(e) => setPassword(e.target.value)} aria-describedby="login-error" />
        </div>
        <p id="login-error" className="form-error" role="alert">{error}</p>
        <button className="btn btn-gold" type="submit" disabled={busy || password.length === 0}>{busy ? "กำลังตรวจสอบ…" : "เข้าสู่ระบบ"}</button>
        </div>
      </form>
    </>
  );
}
