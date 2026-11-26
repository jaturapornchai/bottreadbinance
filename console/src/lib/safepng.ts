import fs from "node:fs/promises";
import path from "node:path";

const PNG_MAGIC = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const MAX_PNG = 12 * 1024 * 1024;
const ci = process.platform === "win32";   // Windows paths are case-insensitive, Linux ones are not
const norm = (s: string) => (ci ? s.toLowerCase() : s);

/** Port of Snapshot.SafePng: only *.png that resolve to a place under <root>/reports/ (md text includes model output, so never trust it). */
export function safePng(root: string, baseDir: string, rel: string | null | undefined): string | null {
  if (!rel || rel.includes("\0")) return null;
  const reports = path.join(root, "reports") + path.sep;
  const full = path.resolve(baseDir, rel);
  return norm(full).startsWith(norm(reports)) && norm(full).endsWith(".png") ? full : null;
}

/**
 * The plan stores the path as the writer saw it (D:\bottreadbinance\reports\charts\x.png on the PC, /app/reports/charts/x.png in
 * the team container). Here the same folder is mounted elsewhere, so keep the part from "reports/" on and re-root it.
 */
export function pngRel(raw: string): string | null {
  const s = raw.replace(/\\/g, "/");
  if (s.startsWith("reports/")) return s;
  const i = s.lastIndexOf("/reports/");
  return i >= 0 ? s.slice(i + 1) : null;
}

/** The file bytes to serve (after SafePng, symlink check, size and PNG signature check) or null. */
export async function openPng(root: string, raw: string): Promise<Buffer | null> {
  const rel = pngRel(raw);
  const full = rel ? safePng(root, root, rel) : null;
  if (!full) return null;
  try {
    const [real, realReports] = await Promise.all([fs.realpath(full), fs.realpath(path.join(root, "reports"))]);
    if (!norm(real).startsWith(norm(realReports + path.sep))) return null;   // a symlink out of reports/
    const st = await fs.stat(real);
    if (!st.isFile() || st.size > MAX_PNG) return null;
    const buf = await fs.readFile(real);
    return buf.subarray(0, 8).equals(PNG_MAGIC) ? buf : null;
  } catch { return null; }
}
