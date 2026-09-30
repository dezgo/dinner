// Mirror of app/services/money.split_cents, for previews only ("if you join,
// everyone pays …"). The server always does the real calculation.
export function splitCents(total, weights) {
  const sum = weights.reduce((a, [, w]) => a + w, 0);
  const out = {};
  if (!sum) return out;
  const sign = total < 0 ? -1 : 1;
  const mag = Math.abs(total);
  weights.forEach(([k, w]) => { out[k] = Math.floor((mag * w) / sum); });
  let left = mag - Object.values(out).reduce((a, b) => a + b, 0);
  const order = weights
    .map(([k, w], i) => ({ k, r: (mag * w) % sum, i }))
    .sort((a, b) => b.r - a.r || a.i - b.i);
  for (const { k } of order) { if (left-- <= 0) break; out[k] += 1; }
  for (const k in out) out[k] *= sign;
  return out;
}
