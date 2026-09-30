// Shared helpers for every screen. No framework: small functions, template
// strings, and esc() on every piece of user or menu text that is rendered.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

export function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

export function money(cents) {
  if (cents === null || cents === undefined) return "—";
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}$${Math.floor(abs / 100).toLocaleString("en-AU")}.${String(abs % 100).padStart(2, "0")}`;
}

// "12.50" / "$12" -> 1250; blank or junk -> null
export function toCents(text) {
  const t = String(text ?? "").replace(/[$,\s]/g, "");
  if (!/^-?\d+(\.\d{1,2})?$/.test(t)) return null;
  const neg = t.startsWith("-");
  const [d, f = ""] = t.replace("-", "").split(".");
  const c = parseInt(d, 10) * 100 + parseInt(f.padEnd(2, "0"), 10);
  return neg ? -c : c;
}
export const centsToInput = (c) => (c === null || c === undefined ? "" : (c / 100).toFixed(2));

export function uid() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "");
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : detail?.message || `Error ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

// All state-changing calls carry X-DinnerTab: the server refuses them otherwise
// (a cross-site page can't add that header without a preflight we never allow).
export async function api(method, url, body, { form } = {}) {
  const opts = { method, headers: { "X-DinnerTab": "1" }, credentials: "same-origin", cache: "no-store" };
  if (form) opts.body = form;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    throw new ApiError(0, "No connection. Check your signal.");
  }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new ApiError(res.status, data?.detail ?? `Error ${res.status}`);
  return data;
}

// ---------------------------------------------------------------- outbox
// Changes are queued, sent in order and retried until the server answers.
// Each carries an op id so a retry after a dropped reply is never applied twice.
// The queue survives a refresh (and a trip to the banking app) in localStorage.
export class Outbox {
  constructor(key, { onChange, onDone, onRejected }) {
    this.key = key;
    this.onChange = onChange;
    this.onDone = onDone;
    this.onRejected = onRejected;
    this.ops = this._load();
    this.busy = false;
    this.failing = false;
    this.delay = 1000;
  }
  _load() {
    try { return JSON.parse(localStorage.getItem(this.key) || "[]"); } catch { return []; }
  }
  _save() {
    try { localStorage.setItem(this.key, JSON.stringify(this.ops)); } catch { /* memory only */ }
    this.onChange?.(this);
  }
  get size() { return this.ops.length; }
  push(op) {
    op.id = op.id || uid();
    if (op.body && typeof op.body === "object") op.body.op_id = op.id;
    if (op.method === "DELETE") op.url += (op.url.includes("?") ? "&" : "?") + "op_id=" + op.id;
    this.ops.push(op);
    this._save();
    this.flush();
    return op.id;
  }
  async flush() {
    if (this.busy) return;
    this.busy = true;
    while (this.ops.length) {
      const op = this.ops[0];
      try {
        const result = await api(op.method, op.url, op.body);
        this.ops.shift();
        this.failing = false;
        this.delay = 1000;
        this._save();
        this.onDone?.(op, result);
      } catch (e) {
        if (e.status === 0 || e.status >= 500 || e.status === 429) {
          this.failing = true;
          this.onChange?.(this);
          setTimeout(() => { this.busy = false; this.flush(); }, this.delay);
          this.delay = Math.min(this.delay * 2, 20000);
          return;
        }
        // The server said no (conflict, not allowed…). Drop it and say why.
        this.ops.shift();
        this._save();
        this.onRejected?.(op, e);
      }
    }
    this.busy = false;
  }
}

// ---------------------------------------------------------------- live
// Server-Sent Events carry only "dinner X is now at revision N"; the page then
// re-fetches what it's allowed to see. Reconnects automatically.
export function live(url, { onRevision, onStatus }) {
  let es;
  let last = 0;
  const open = () => {
    es = new EventSource(url);
    es.onopen = () => { onStatus?.(true); onRevision?.(null); };
    es.onerror = () => onStatus?.(false);
    es.onmessage = (m) => {
      try {
        const d = JSON.parse(m.data);
        if (!d.revision || d.revision > last) { last = d.revision || last; onRevision?.(d); }
      } catch { /* ignore */ }
    };
  };
  open();
  // Coming back from the banking app: make sure we're current.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      onRevision?.(null);
      if (es.readyState === EventSource.CLOSED) open();
    }
  });
  return () => es.close();
}

// ---------------------------------------------------------------- ui bits
let toastTimer;
export function toast(message, bad = false) {
  let el = $("#toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.setAttribute("role", "status");
    document.body.append(el);
  }
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3000);
}

export function sheet(html, { onClose } = {}) {
  const back = document.createElement("div");
  back.className = "sheet-back";
  back.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">${html}</div>`;
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); onClose?.(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  back.addEventListener("click", (e) => { if (e.target === back || e.target.closest("[data-close]")) close(); });
  document.addEventListener("keydown", onKey);
  document.body.append(back);
  back.querySelector("input,select,textarea,button:not([data-close])")?.focus({ preventScroll: true });
  return { el: back.firstElementChild, close };
}

export async function copy(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`);
  } catch {
    // Older browsers: select a temporary field.
    const t = document.createElement("textarea");
    t.value = text;
    document.body.append(t);
    t.select();
    try { document.execCommand("copy"); toast(`${what} copied`); } catch { toast(`Couldn't copy — ${text}`, true); }
    t.remove();
  }
}

export function explainError(e) {
  if (e?.detail?.issues) return e.detail.message;
  return e?.message || "Something went wrong.";
}

// Theme and text size controls (per device).
export function prefsControls(root) {
  const setFs = (delta) => {
    const cur = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--fs"), 10) || 17;
    const next = Math.max(14, Math.min(26, cur + delta));
    document.documentElement.style.setProperty("--fs", next + "px");
    try { localStorage.setItem("dt_fs", String(next)); } catch { /* fine */ }
  };
  const cycleTheme = () => {
    const cur = document.documentElement.dataset.theme;
    const dark = cur ? cur === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("dt_theme", next); } catch { /* fine */ }
  };
  root.innerHTML = `
    <button class="iconbtn" data-fs="-1" aria-label="Smaller text">A−</button>
    <button class="iconbtn" data-fs="1" aria-label="Larger text">A+</button>
    <button class="iconbtn" data-theme-toggle aria-label="Switch light or dark">◐</button>`;
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.fs) setFs(parseInt(b.dataset.fs, 10));
    if (b.hasAttribute("data-theme-toggle")) cycleTheme();
  });
}

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("en-AU", { weekday: "short", hour: "numeric", minute: "2-digit", day: "numeric", month: "short" });
}
