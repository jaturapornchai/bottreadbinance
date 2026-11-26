export function normalizePercentages(weights: readonly number[]): number[] {
  if (weights.length === 0) return [];
  const safe = weights.map((w) => (Number.isFinite(w) && w > 0 ? w : 1));
  const sum = safe.reduce((a, b) => a + b, 0);
  if (sum <= 0) return weights.map(() => 100 / weights.length);
  const raw = safe.map((w) => (w / sum) * 100);
  const rounded = raw.map((v) => Math.round(v * 100) / 100);
  const diff = Math.round((100 - rounded.reduce((a, b) => a + b, 0)) * 100) / 100;
  rounded[rounded.length - 1] = Math.max(1, Math.round((rounded[rounded.length - 1] + diff) * 100) / 100);
  return rounded;
}

export function redistributeColumnPercentages(
  current: readonly number[],
  index: number,
  deltaPct: number,
  minPcts: readonly number[],
): number[] {
  const n = current.length;
  if (n <= 1 || index < 0 || index >= n) return [...current];
  const next = [...current];
  const minSelf = minPcts[index] ?? 4;
  const neighbor = index < n - 1 ? index + 1 : index - 1;
  const minNeighbor = minPcts[neighbor] ?? 4;
  const maxGrow = next[neighbor] - minNeighbor;
  const maxShrink = next[index] - minSelf;
  let applied = deltaPct;
  if (applied > 0) applied = Math.min(applied, Math.max(0, maxGrow));
  else applied = -Math.min(-applied, Math.max(0, maxShrink));

  // If immediate neighbor hits its floor while growing, borrow smoothly from remaining columns
  if (deltaPct > 0 && applied < deltaPct) {
    let rem = deltaPct - applied;
    next[index] += applied;
    next[neighbor] -= applied;
    for (let i = n - 1; i >= 0 && rem > 0.01; i--) {
      if (i === index || i === neighbor) continue;
      const avail = Math.max(0, next[i] - (minPcts[i] ?? 4));
      const take = Math.min(avail, rem);
      next[i] -= take;
      next[index] += take;
      rem -= take;
    }
  } else {
    next[index] += applied;
    next[neighbor] -= applied;
  }
  return normalizePercentages(next);
}
