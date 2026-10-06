// Organiser app: dashboard, settings, and one dinner's workspace.
import { $, $$, api, ApiError, centsToInput, copy, esc, explainError, live, money, prefsControls, sheet, toast, toCents, when } from "./core.js";

const app = $("#app");
const view = document.body.dataset.view;
const dinnerId = document.body.dataset.dinner;

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
prefsControls($("#prefs"));

async function call(method, url, body, opts) {
  try {
    return await api(method, url, body, opts);
  } catch (e) {
    if (e.status === 401) { location.href = "/o/login"; throw e; }
    throw e;
  }
}
const fail = (e) => toast(explainError(e), true);

// ================================================================ home
const dayOf = (iso) => new Date(iso).toLocaleDateString("en-AU", { day: "numeric", month: "short", year: "numeric" });
const nameKey = (t) => String(t || "").toLowerCase().split(/\s+/).filter(Boolean).join(" ");

async function home() {
  const [data, known] = await Promise.all([call("GET", "/api/o/dinners"), call("GET", "/api/o/restaurants")]);
  app.innerHTML = `
    <div class="wrap">
      <div class="row between" style="margin:1rem 0"><h1 style="margin:0">Dinners</h1><a href="/o/settings" class="btn">Settings</a></div>
      ${!data.profile_ready ? `<div class="banner warn">Add your PayID in <a href="/o/settings">Settings</a> before finalising a real bill.</div>` : ""}
      ${data.review_count ? `<div class="banner warn">${data.review_count} incoming transfer${data.review_count > 1 ? "s need" : " needs"} your review — open the dinner's Payments tab.</div>` : ""}
      <form id="new" class="card stack">
        <h2>New dinner</h2>
        <div><label for="rn">Restaurant</label><input id="rn" maxlength="80" placeholder="e.g. Lantern Kitchen" list="rlist" autocomplete="off">
          <datalist id="rlist">${known.restaurants.map((r) => `<option value="${esc(r.name)}">`).join("")}</datalist></div>
        <div id="rsaved"></div>
        <div><label for="tn">Table (optional)</label><input id="tn" maxlength="20" placeholder="e.g. 12" style="max-width:10rem"></div>
        <button class="primary block">Start dinner</button>
      </form>
      <section>
        ${data.dinners.map((d) => `
          <a class="card row between" href="/o/d/${d.id}" style="display:flex;text-decoration:none;color:inherit;margin-top:.75rem">
            <div class="grow"><b>${esc(d.restaurant_name || "Dinner")}</b>${d.table_label ? ` · Table ${esc(d.table_label)}` : ""} <span class="muted">${esc(d.code)}</span>
              <div class="muted">${when(d.created_at)} · ${d.people} people${d.is_demo ? " · demo" : ""}</div></div>
            <span class="state ${d.status === "finalised" ? (d.waiting_on ? "warn" : "good") : "info"}">${d.status === "finalised" ? (d.waiting_on ? `Waiting on ${d.waiting_on}` : "All paid") : "Open"}</span>
          </a>`).join("") || '<p class="muted">No dinners yet.</p>'}
      </section>
      ${data.archived_count ? `<p class="center"><button class="small" id="showcleared">Cleared dinners (${data.archived_count})</button></p><section id="cleared"></section>` : ""}
      <section class="card stack">
        <h2>Try it first</h2>
        <p class="muted">A demo dinner with a sample menu, guests, orders and receipt. Payments are simulated — nothing touches your bank.</p>
        <button id="demo" class="block">Create demo dinner</button>
      </section>
      <p class="center"><button class="small" id="out">Sign out</button></p>
    </div>`;
  // Been here before? Offer the menu saved last time.
  $("#rn").addEventListener("input", () => {
    const r = known.restaurants.find((x) => nameKey(x.name) === nameKey($("#rn").value));
    $("#rsaved").innerHTML = r?.dishes ? `<label class="check"><input type="checkbox" id="usesaved" checked>
      Start with the menu saved ${dayOf(r.menu_updated_at)} (${r.dishes} dishes) — guests can see it straight away</label>` : "";
  });
  $("#new").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await call("POST", "/api/o/dinners", { restaurant_name: $("#rn").value, table_label: $("#tn").value, use_saved_menu: $("#usesaved")?.checked ?? true });
      location.href = `/o/d/${r.id}`;
    } catch (err) { fail(err); }
  });
  $("#demo").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { const r = await call("POST", "/api/o/demo"); location.href = `/o/d/${r.id}`; } catch (err) { fail(err); e.target.disabled = false; }
  });
  $("#out").addEventListener("click", async () => { await api("POST", "/api/logout"); location.href = "/o/login"; });
  $("#showcleared")?.addEventListener("click", async (e) => {
    e.target.remove();
    try { showCleared((await call("GET", "/api/o/dinners?archived=true")).dinners); } catch (err) { fail(err); }
  });
}

function showCleared(list) {
  const box = $("#cleared");
  box.innerHTML = `<h2>Cleared dinners</h2><p class="muted">Hidden from your list and closed to guests. Bring one back to use it again.</p>
    ${list.map((d) => `<div class="card row between" style="margin-top:.5rem">
      <div class="grow"><b>${esc(d.restaurant_name || "Dinner")}</b> <span class="muted">${esc(d.code)}</span>
        <div class="muted">${when(d.created_at)} · ${d.people} people${d.is_demo ? " · demo" : ""}</div></div>
      <button class="small" data-restore="${d.id}">Bring back</button></div>`).join("")}`;
  box.addEventListener("click", async (e) => {
    const id = e.target.closest("[data-restore]")?.dataset.restore;
    if (!id) return;
    try { await call("PATCH", `/api/o/d/${id}`, { archived: false }); location.href = `/o/d/${id}`; } catch (err) { fail(err); }
  });
}

// ============================================================ settings
async function settings() {
  const d = await call("GET", "/api/o/settings");
  const p = d.profile || {};
  const u = d.up;
  app.innerHTML = `
    <div class="wrap">
      <h1 style="margin-top:1rem">Settings</h1>
      <form id="prof" class="card stack">
        <h2>Where guests pay you</h2>
        <div><label for="myname">Your name at dinners</label><input id="myname" maxlength="40" value="${esc(p.my_name || "")}" placeholder="Derek"></div>
        <div class="field-row">
          <div><label for="ptype">PayID type</label><select id="ptype">
            ${[["phone", "Mobile number"], ["email", "Email"], ["abn", "ABN"], ["org_id", "Organisation ID"]].map(([v, l]) => `<option value="${v}" ${p.payid_type === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
          <div><label for="payid">PayID</label><input id="payid" value="${esc(p.payid || "")}" autocomplete="off"></div>
        </div>
        <div><label for="rname">Name guests should see when paying</label><input id="rname" value="${esc(p.recipient_name || "")}" placeholder="As your bank shows it"></div>
        <details><summary>BSB and account number (fallback)</summary>
          <div class="field-row"><div><label for="bsb">BSB</label><input id="bsb" inputmode="numeric" value="${esc(p.bsb || "")}"></div>
          <div><label for="acct">Account number</label><input id="acct" inputmode="numeric" value="${esc(p.account_number || "")}"></div></div>
        </details>
        <button class="primary block">Save</button>
      </form>

      <section class="card stack">
        <h2>Up Bank — automatic payment detection</h2>
        ${!u.token_configured ? `<div class="banner info">Not connected. Put your Up personal access token in the server's <code>.env</code> as <code>UP_API_TOKEN</code> and restart. Until then, mark payments as received by hand — everything else works the same.</div>` : `
          <div class="stat">
            <dt>Receiving account</dt><dd>${esc(u.account_name || "not chosen")}</dd>
            <dt>Webhook</dt><dd>${u.webhook_id ? "registered" : "not registered"}</dd>
            <dt>Last check</dt><dd>${u.last_sync ? when(u.last_sync.at) : "never"}</dd>
          </div>
          ${u.last_error ? `<div class="banner bad">Last error: ${esc(u.last_error.message)}</div>` : ""}
          <div class="row"><button id="accts">Choose receiving account</button><button id="hook">${u.webhook_id ? "Re-register" : "Register"} webhook</button><button id="sync">Check now</button></div>
          <p class="muted">Webhook address: <code>${esc(u.suggested_webhook_url)}</code> — must be public HTTPS. Without it, the app still checks Up every few minutes while payments are due.</p>
          <div id="acctlist"></div>`}
      </section>

      <section class="card">
        <h2>Menu and receipt reading</h2>
        <p>${d.extraction_enabled ? `On — photos are read by ${esc(d.extraction_model)}.` : "Off — no Anthropic API key on the server. Menus and receipts can still be typed in by hand (the demo's sample photos still work)."}</p>
      </section>
      <p><a href="/o">← Dinners</a></p>
    </div>`;
  $("#prof").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await call("PUT", "/api/o/settings/profile", {
        my_name: $("#myname").value, payid_type: $("#ptype").value, payid: $("#payid").value,
        recipient_name: $("#rname").value, bsb: $("#bsb").value, account_number: $("#acct").value,
      });
      toast("Saved");
    } catch (err) { fail(err); }
  });
  $("#accts")?.addEventListener("click", async () => {
    try {
      const r = await call("GET", "/api/o/up/accounts");
      $("#acctlist").innerHTML = r.accounts.map((a) => `<button class="block" data-acct="${a.id}" style="margin-top:.4rem">${esc(a.name)} <span class="muted">${esc(a.type.toLowerCase())}${a.ownership === "JOINT" ? " · 2Up" : ""}</span></button>`).join("");
    } catch (err) { fail(err); }
  });
  $("#acctlist")?.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-acct]");
    if (!b) return;
    try { await call("PUT", "/api/o/up/account", { id: b.dataset.acct }); settings(); } catch (err) { fail(err); }
  });
  $("#hook")?.addEventListener("click", async () => {
    try { await call("POST", "/api/o/up/webhook"); toast("Webhook registered and pinged"); settings(); } catch (err) { fail(err); }
  });
  $("#sync")?.addEventListener("click", async () => {
    try { const r = await call("POST", "/api/o/up/sync"); toast(r.skipped || `Checked ${r.credits_checked} incoming transfer(s)`); settings(); } catch (err) { fail(err); }
  });
}

// ============================================================== dinner
let S = null;
let dtab = "share";
try { dtab = sessionStorage.getItem(`dt_otab_${dinnerId}`) || "share"; } catch { /* fine */ }
const openEditors = new Set();

const nameOf = (id) => S.participants.find((p) => p.id === id)?.name || "?";
const lineCalc = (l) => S.bill.lines[l.id] || { amount: 0, shares: {}, unallocated: 0 };
const locked = () => S.dinner.status === "finalised";
const gBase = () => `/api/d/${S.public_token}`;
const oBase = `/api/o/d/${dinnerId}`;

let refreshTimer;
const refreshSoon = () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 150); };
async function refresh() {
  // Don't yank the page out from under a half-typed form.
  // Nor while the camera or photo picker is open — the photo comes back to this page.
  if (document.activeElement?.closest("form[data-keep]") || photoPicking) { refreshTimer = setTimeout(refresh, 1500); return; }
  S = await call("GET", `${oBase}/state`);
  renderDinner();
}

function renderDinner() {
  document.title = `${S.dinner.restaurant_name || "Dinner"} — organiser`;
  $("#title").textContent = `${S.dinner.restaurant_name || "Dinner"}${S.dinner.table_label ? ` · Table ${S.dinner.table_label}` : ""}`;
  const needsReview = S.bank_review.filter((t) => t.match_state === "review").length;
  const tabs = [
    ["share", "Share"], ["menu", `Menu${S.menu.pages.some((p) => p.status === "review") ? " •" : ""}`], ["bill", "Bill"],
    ["receipt", "Receipt"], ["pay", `Payments${needsReview ? ` (${needsReview})` : ""}`], ["log", "Log"],
  ];
  const scroll = window.scrollY;
  app.innerHTML = `
    <div class="wrap">
      <p style="margin:.75rem 0 0"><a href="/o">← All dinners / start a new one</a></p>
      <div class="row between" style="margin-top:.5rem">
        <div><b>${esc(S.dinner.code)}</b> · <span class="state ${locked() ? "good" : S.dinner.reopened ? "warn" : "info"}">${locked() ? "Finalised" : S.dinner.reopened ? "Reopened" : "Open"}</span>${S.dinner.is_demo ? ' <span class="state">Demo</span>' : ""}</div>
        <a class="btn small" href="/d/${esc(S.public_token)}">My diner view</a>
      </div>
      <nav class="subtabs" aria-label="Dinner sections">${tabs.map(([k, l]) => `<button class="chip" data-dtab="${k}" aria-pressed="${dtab === k}">${l}</button>`).join("")}</nav>
      <div id="pane"></div>
    </div>`;
  ({ share: paneShare, menu: paneMenu, bill: paneBill, receipt: paneReceipt, pay: panePay, log: paneLog }[dtab] || paneShare)();
  window.scrollTo(0, scroll);
}

// ------------------------------------------------------------- share
function paneShare() {
  $("#pane").innerHTML = `
    <section class="card center stack">
      <img class="qr" src="${oBase}/qr.svg?r=${S.dinner.revision}" alt="QR code for this dinner">
      <p><b>“Scan this to see the menu, record what you order, and help sort out the bill.”</b></p>
      <div class="row" style="justify-content:center"><code style="word-break:break-all">${esc(S.share_url)}</code></div>
      <button class="small" data-copy="${esc(S.share_url)}">Copy link</button>
    </section>
    <section class="card stack">
      <h2 style="margin:0">Restaurant and table</h2>
      <form id="rename" data-keep class="row"><input id="rest" class="grow" value="${esc(S.dinner.restaurant_name)}" placeholder="Restaurant" aria-label="Restaurant">
        <input id="tbl" value="${esc(S.dinner.table_label)}" placeholder="Table" aria-label="Table" maxlength="20" style="width:6.5rem"><button>Save</button></form>
    </section>
    <section class="card">
      <h2>People (${S.participants.length})</h2>
      <table class="plain">
        ${S.participants.map((p) => `<tr><td><b>${esc(p.name)}</b>${p.is_organiser ? " (you)" : ""}<div class="muted">${esc(p.reference)}${p.has_session || p.is_organiser ? "" : " · no phone linked"}</div></td>
          <td class="right">${p.is_organiser ? "" : `<button class="small" data-plink="${p.id}">Personal link</button> <button class="small" data-prename="${p.id}">Rename</button> <button class="small danger" data-premove="${p.id}">Remove</button>`}</td></tr>`).join("")}
      </table>
      <form id="addp" data-keep class="row" style="margin-top:.75rem"><input id="pname" class="grow" placeholder="Add someone without a phone" maxlength="40"><button>Add</button></form>
      <p class="muted">Duplicate names are fine — each person gets their own reference. If someone changes phone, give them a personal link (it works once, for 12 hours).</p>
    </section>
    <section class="card stack">
      <h2>Finished with this dinner?</h2>
      <p class="muted">Clear it off your list to start fresh. Nothing is deleted — you can bring it back from “Cleared dinners” on the dinner list.</p>
      <button class="danger block" data-cleardinner>Clear this dinner</button>
    </section>`;
  $("#rename").addEventListener("submit", async (e) => { e.preventDefault(); try { await call("PATCH", oBase, { restaurant_name: $("#rest").value, table_label: $("#tbl").value }); toast("Saved"); document.activeElement?.blur(); refresh(); } catch (err) { fail(err); } });
  $("#addp").addEventListener("submit", async (e) => { e.preventDefault(); try { await call("POST", `${oBase}/people`, { name: $("#pname").value }); $("#pname").value = ""; refresh(); } catch (err) { fail(err); } });
}

// -------------------------------------------------------------- menu
const DIETS = [["vegetarian", "Vegetarian"], ["vegan", "Vegan"], ["gluten_free", "Gluten free"], ["dairy_free", "Dairy free"], ["nut_free", "Nut free"], ["contains_nuts", "Contains nuts"], ["halal", "Halal"], ["spicy", "Spicy"]];
const PAGE_STATE = { processing: ["Reading…", "info"], review: ["Needs review", "warn"], published: ["Live for guests", "good"], failed: ["Couldn't read", "bad"] };

// ------------------------------------------------------------- photo trays
// A phone's camera takes one shot per tap, so photos collect in a tray until
// they're sent together. The tray outlives re-renders (live updates redraw the pane).
const TRAY_MAX = { menu: 12, receipt: 4 }; // the server keeps no more than this per upload
const trays = { menu: [], receipt: [] };
let photoPicking = false;
// Fallback for browsers without the input "cancel" event: give up waiting once the page is back.
window.addEventListener("focus", () => { if (photoPicking) setTimeout(() => { photoPicking = false; }, 3000); });

function trayHtml(key) {
  const t = trays[key];
  const full = t.length >= TRAY_MAX[key];
  return `<div class="stack" data-tray="${key}">
    ${t.length ? `<div class="tray">${t.map((p, i) => `<div class="tray-item">${p.pdf ? `<div class="thumb pdf-thumb" title="${esc(p.file.name)}">PDF</div>` : `<img class="thumb" src="${p.url}" alt="Photo ${i + 1}">`}<button type="button" class="small" data-trayremove="${i}" aria-label="Remove photo ${i + 1}">✕</button></div>`).join("")}</div>` : ""}
    ${full ? `<p class="muted">That's the most you can send at once (${TRAY_MAX[key]}). Send these, then add more.</p>` : `<div class="row">
      <label class="btn grow">${t.length ? "Take another photo" : "Take photo"}<input type="file" accept="image/*" capture="environment" hidden data-trayadd></label>
      <label class="btn grow">Choose photos or PDF<input type="file" accept="image/*,application/pdf,.pdf" multiple hidden data-trayadd></label>
    </div>`}
  </div>`;
}

// Draw the tray into its placeholder and keep `onChange` (e.g. the send button's label) in step.
function wireTray(key, onChange) {
  const box = $(`[data-tray="${key}"]`);
  if (!box) return;
  box.outerHTML = trayHtml(key);
  const fresh = $(`[data-tray="${key}"]`);
  fresh.querySelectorAll("[data-trayadd]").forEach((inp) => {
    inp.addEventListener("click", () => { photoPicking = true; });
    inp.addEventListener("cancel", () => { photoPicking = false; });
  });
  fresh.querySelectorAll("[data-trayadd]").forEach((inp) => inp.addEventListener("change", () => {
    photoPicking = false;
    const room = TRAY_MAX[key] - trays[key].length;
    const picked = [...inp.files];
    if (picked.length > room) toast(`Only ${room} more fit in one go — send these first, then add the rest.`, true);
    for (const f of picked.slice(0, room)) trays[key].push({ file: f, url: URL.createObjectURL(f), pdf: f.type === "application/pdf" || /\.pdf$/i.test(f.name) });
    wireTray(key, onChange);
  }));
  fresh.querySelectorAll("[data-trayremove]").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    const [gone] = trays[key].splice(Number(b.dataset.trayremove), 1);
    URL.revokeObjectURL(gone.url);
    wireTray(key, onChange);
  }));
  onChange(trays[key].length);
}

function emptyTray(key) {
  trays[key].forEach((p) => URL.revokeObjectURL(p.url));
  trays[key] = [];
}

function paneMenu() {
  const m = S.menu;
  const cats = m.categories;
  const itemsFor = (pid) => m.items.filter((i) => i.page_id === pid);
  const manual = m.items.filter((i) => !i.page_id);
  const fromSaved = m.pages.some((p) => p.from_saved);
  const rescanned = m.pages.some((p) => !p.from_saved && p.kind === "menu" && ["review", "published"].includes(p.status));
  const unseen = m.items.filter((i) => i.from_saved);
  const savedNote = !fromSaved ? "" : !rescanned
    ? `<div class="banner info">This is the menu saved from your last visit${S.restaurant?.menu_updated_at ? ` (${dayOf(S.restaurant.menu_updated_at)})` : ""} — guests can already see it.
        If the menu in front of you looks different, photograph it again: matching dishes are updated, new ones added, and each change is noted.</div>`
    : unseen.length ? `<div class="banner warn"><b>${unseen.length} saved dish${unseen.length > 1 ? "es weren't" : " wasn't"} on today's scan:</b> ${unseen.map((i) => esc(i.name)).join(", ")}.
        If you photographed the whole menu, ${unseen.length > 1 ? "they've" : "it's"} probably gone.
        <div class="row" style="margin-top:.5rem"><button class="small primary" data-dropunseen>Remove ${unseen.length > 1 ? "them" : "it"}</button></div></div>` : "";
  $("#pane").innerHTML = `
    ${savedNote}
    <section class="card stack">
      <h2>${fromSaved ? "Scan the menu again" : "Add menu photos"}</h2>
      <p class="muted">Several pages? Take them one after another — they wait here until you send them. Found the menu online? Choose its PDF instead — every page is read.</p>
      <form id="up" class="stack">
        <div data-tray="menu"></div>
        <div class="row"><select id="kind" style="width:auto"><option value="menu">Menu pages</option><option value="specials">Specials / tonight only</option></select>
        <button class="primary grow" id="upbtn">Upload</button></div>
      </form>
      ${!S.extraction_enabled ? `<div class="banner info">Photo reading is off on this server, so you'll type dishes in below. ${S.dinner.is_demo ? "Sample photos from the demo still read." : ""}</div>` : ""}
      <p class="muted">Share the QR code straight away — guests see each page as soon as you publish it.</p>
    </section>
    ${m.pages.map((p) => {
      const [label, tone] = PAGE_STATE[p.status] || [p.status, ""];
      const items = itemsFor(p.id);
      const flagged = items.filter((i) => i.flags.length).length;
      return `<section class="card">
        <div class="row">
          ${p.image ? `<a href="${gBase()}/image/${esc(p.image)}" target="_blank" rel="noopener"><img class="thumb" src="${gBase()}/image/${esc(p.image)}" alt="Menu page"></a>` : ""}
          <div class="grow"><b>${p.kind === "specials" ? "Specials" : p.from_saved ? "Saved menu page" : "Menu page"}</b> <span class="state ${tone}">${p.status === "processing" ? '<span class="spinner"></span> ' : ""}${label}</span>
            <div class="muted">${items.length} dishes${flagged ? ` · <b>${flagged} flagged</b>` : ""}</div>
            ${p.error ? `<div class="banner bad">${esc(p.error)}</div>` : ""}
            ${(p.flags || []).map((f) => `<div class="muted">⚠ ${esc(f)}</div>`).join("")}</div>
        </div>
        <div class="row" style="margin-top:.5rem">
          ${p.status === "review" ? `<button class="primary small" data-page="${p.id}" data-act="publish">Publish to guests</button>` : ""}
          ${p.status === "published" ? `<button class="small" data-page="${p.id}" data-act="unpublish">Hide from guests</button>` : ""}
          ${p.status === "failed" ? `<button class="small" data-page="${p.id}" data-act="retry">Try again</button>` : ""}
          <button class="small danger" data-delpage="${p.id}">Delete page</button>
        </div>
        ${items.length ? `<details ${p.status === "review" ? "open" : ""}><summary>Review dishes</summary>${items.map(itemRow).join("")}</details>` : ""}
      </section>`;
    }).join("")}
    <section class="card">
      <h2>Typed in by hand${manual.length ? ` (${manual.length})` : ""}</h2>
      ${manual.map(itemRow).join("")}
      <form id="additem" data-keep class="stack" style="margin-top:.75rem">
        <h3>Add a dish or special</h3>
        <div><label for="ni">Name</label><input id="ni" required maxlength="120"></div>
        <div class="field-row">
          <div><label for="np">Price</label><input id="np" inputmode="decimal" placeholder="blank if unknown"></div>
          <div><label for="nc">Section</label><select id="nc"><option value="">—</option>${cats.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join("")}</select></div>
        </div>
        <div><label for="nd">Description (as printed)</label><input id="nd" maxlength="400"></div>
        <label class="check"><input type="checkbox" id="nsp"> Tonight's special</label>
        <button class="primary">Add dish</button>
      </form>
      <form id="addcat" data-keep class="row" style="margin-top:.75rem"><input id="ncat" class="grow" placeholder="New section name"><button>Add section</button></form>
    </section>`;

  wireTray("menu", (n) => {
    $("#upbtn").disabled = !n;
    $("#upbtn").textContent = n > 1 ? `Upload ${n} files` : "Upload";
  });
  $("#up").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!trays.menu.length) { toast("Choose or take a photo first.", true); return; }
    const fd = new FormData();
    for (const p of trays.menu) fd.append("files", p.file);
    fd.append("kind", $("#kind").value);
    $("#upbtn").disabled = true;
    $("#upbtn").innerHTML = '<span class="spinner"></span> Uploading';
    try { await call("POST", `${oBase}/pages`, undefined, { form: fd }); emptyTray("menu"); toast("Uploaded — reading now"); } catch (err) { fail(err); }
    refresh();
  });
  $("#additem").addEventListener("submit", async (e) => {
    e.preventDefault();
    const price = $("#np").value.trim() ? toCents($("#np").value) : null;
    if ($("#np").value.trim() && price == null) { toast("Price like 24.50", true); return; }
    try {
      await call("POST", `${oBase}/items`, { name: $("#ni").value, price_cents: price, description: $("#nd").value,
        category_id: $("#nc").value || null, is_special: $("#nsp").checked });
      e.target.reset();
      refresh();
    } catch (err) { fail(err); }
  });
  $("#addcat").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await call("POST", `${oBase}/categories`, { name: $("#ncat").value }); refresh(); } catch (err) { fail(err); }
  });
}

function optsText(list) { return (list || []).map((o) => `${o.label} = ${o.price_cents != null ? (o.price_cents / 100).toFixed(2) : "?"}`).join("\n"); }
function parseOpts(text) {
  return text.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
    const [label, price] = l.split("=").map((x) => x.trim());
    return { label, price_cents: price && price !== "?" ? toCents(price) : null };
  });
}

function itemRow(i) {
  const open = openEditors.has(i.id);
  const cat = S.menu.categories.find((c) => c.id === i.category_id);
  const price = i.variants.length ? i.variants.map((v) => `${esc(v.label)} ${money(v.price_cents)}`).join(" · ") : money(i.price_cents);
  const v = i.vegan?.status;
  return `<div class="line">
    <div class="grow">
      <b>${esc(i.name)}</b> <span class="muted">${esc(cat?.name || "")}</span>
      <div class="muted">${price}${i.price_text ? ` · printed “${esc(i.price_text)}”` : ""}</div>
      ${i.change_note ? `<div><span class="badge special">${i.change_note === "New" ? "New since last visit" : esc(i.change_note)}</span></div>` : ""}
      <div class="badges">${i.labels.map((l) => `<span class="badge">${esc(l)}</span>`).join("")}
        ${v === "marked" ? '<span class="badge vegan">Vegan (menu)</span>' : v === "on_request" ? '<span class="badge vegan">Vegan on request</span>' : v === "possible" ? '<span class="badge maybe">Possibly vegan (AI)</span>' : ""}
        ${i.unavailable ? '<span class="badge off">Unavailable</span>' : ""}${i.is_special ? '<span class="badge special">Special</span>' : ""}</div>
      ${i.flags.map((f) => `<div class="banner warn" style="margin:.3rem 0">⚠ ${esc(f.message)}</div>`).join("")}
      ${open ? itemEditor(i) : ""}
    </div>
    <div class="stack" style="flex:none">
      <button class="small" data-edititem="${i.id}">${open ? "Close" : "Edit"}</button>
      <button class="small" data-avail="${i.id}">${i.unavailable ? "Available" : "Sold out"}</button>
    </div>
  </div>`;
}

function itemEditor(i) {
  return `<form class="editor stack" data-keep data-itemform="${i.id}">
    <div><label>Name</label><input name="name" value="${esc(i.name)}" required></div>
    <div><label>Description (exactly as printed)</label><textarea name="description">${esc(i.description)}</textarea></div>
    <div class="field-row"><div><label>Price</label><input name="price" inputmode="decimal" value="${centsToInput(i.price_cents)}" placeholder="none"></div>
      <div><label>Section</label><select name="category_id"><option value="">—</option>${S.menu.categories.map((c) => `<option value="${c.id}" ${c.id === i.category_id ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select></div></div>
    <div><label>Sizes / versions — one per line, “Glass = 11.00”</label><textarea name="variants">${esc(optsText(i.variants))}</textarea></div>
    <div><label>Extras — one per line, “Add prawns = 6.00”</label><textarea name="extras">${esc(optsText(i.extras))}</textarea></div>
    <div><label>Dietary symbols as printed (comma separated)</label><input name="labels" value="${esc(i.labels.join(", "))}"></div>
    <fieldset style="border:0;padding:0;margin:0"><legend class="muted">Stated on the menu</legend>
      ${DIETS.map(([k, l]) => `<label class="check"><input type="checkbox" name="diet" value="${k}" ${i.diet.includes(k) ? "checked" : ""}> ${l}</label>`).join("")}</fieldset>
    <div class="field-row"><div><label>Vegan badge</label><select name="vegan">
      ${[["", "None"], ["marked", "Vegan (menu says so)"], ["on_request", "Vegan on request"], ["possible", "Possibly vegan (ask staff)"]].map(([k, l]) => `<option value="${k}" ${(i.vegan?.status || "") === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label>Vegan note</label><input name="vegan_note" value="${esc(i.vegan?.note || "")}" placeholder="e.g. without ghee"></div></div>
    <label class="check"><input type="checkbox" name="special" ${i.is_special ? "checked" : ""}> Tonight's special</label>
    ${i.flags.length ? `<label class="check"><input type="checkbox" name="clearflags"> I've checked the flagged details</label>` : ""}
    <div class="row"><button class="primary grow">Save</button><button type="button" class="danger small" data-delitem="${i.id}">Delete</button></div>
  </form>`;
}

async function saveItem(form) {
  const i = S.menu.items.find((x) => x.id === form.dataset.itemform);
  const f = new FormData(form);
  const priceText = String(f.get("price") || "").trim();
  const price = priceText ? toCents(priceText) : null;
  if (priceText && price == null) { toast("Price like 24.50", true); return; }
  const body = {
    version: i.version, name: f.get("name"), description: f.get("description"), price_cents: price,
    category_id: f.get("category_id") || null, variants: parseOpts(String(f.get("variants"))), extras: parseOpts(String(f.get("extras"))),
    labels: String(f.get("labels")).split(",").map((x) => x.trim()).filter(Boolean), diet: f.getAll("diet"),
    vegan: { status: f.get("vegan") || null, note: f.get("vegan_note") || "" }, is_special: f.get("special") === "on",
  };
  if (f.get("clearflags") === "on") body.flags = [];
  try { await call("PATCH", `${oBase}/items/${i.id}`, body); openEditors.delete(i.id); toast("Saved"); refresh(); } catch (err) { fail(err); refresh(); }
}

// -------------------------------------------------------------- bill
const SPLIT_LABEL = { equal: "Equal", shares: "Unequal shares", units: "Per unit", proportional: "In proportion to items" };

function paneBill() {
  const b = S.bill;
  const diff = b.difference;
  $("#pane").innerHTML = `
    ${S.dinner.reopened ? `<div class="banner warn">Reopened after payment instructions went out. Received payments are kept; when you finalise again, anyone's new balance or overpayment will show.</div>` : ""}
    <section class="card">
      <dl class="stat">
        <dt>Restaurant's total ${b.bill_total_source ? `<small>(${b.bill_total_source === "receipt" ? "from receipt" : "as quoted"})</small>` : ""}</dt><dd>${money(b.bill_total)}</dd>
        <dt>Recorded</dt><dd>${money(b.recorded_total)}</dd>
        <dt>Allocated to people</dt><dd>${money(b.allocated_total)}</dd>
        <dt>Not yet allocated</dt><dd>${money(b.unallocated_total)}</dd>
        ${diff != null ? `<dt>Difference</dt><dd style="color:${diff ? "var(--bad)" : "var(--good)"}">${money(diff)}</dd>` : ""}
      </dl>
      ${locked() ? "" : `<form id="total" data-keep class="row" style="margin-top:.75rem"><input id="tot" class="grow" inputmode="decimal" placeholder="Total the restaurant quoted" value="${centsToInput(S.dinner.bill_total_cents)}"><button>Set total</button></form>
      <p class="muted">No itemised receipt? Enter the total they quote. Scanning a receipt fills this in for you.</p>`}
    </section>
    ${b.accounted_for ? `<div class="banner good"><b>✓ Everything accounted for.</b> Every item is allocated and the recorded bill matches the restaurant's total.</div>`
      : `<div class="banner warn"><b>Still to sort out:</b><ul style="margin:.3rem 0 0 1rem;padding:0">${b.issues.map((i) => `<li>${esc(i.message)}</li>`).join("")}</ul></div>`}
    ${!locked() && diff ? `<button class="block" data-adjust="${diff}">Add a labelled adjustment for ${money(diff)}</button>` : ""}
    <section class="card" style="margin-top:.75rem">
      <div class="row between"><h2 style="margin:0">Items</h2>${locked() ? "" : '<span class="row"><button class="small" data-additem>+ Item</button><button class="small" data-adjust="">+ Surcharge / discount</button></span>'}</div>
      ${S.lines.map(billLine).join("") || '<p class="muted">Nothing recorded yet.</p>'}
    </section>
    <section class="card">
      <h2>Each person's share</h2>
      <table class="plain"><tr><th>Person</th><th class="right">Items</th><th class="right">Adj.</th><th class="right">Share</th></tr>
      ${S.participants.map((p) => { const x = b.people[p.id] || { items: 0, adjustments: 0, total: 0 }; return `<tr><td>${esc(p.name)}</td><td class="right num">${money(x.items)}</td><td class="right num">${money(x.adjustments)}</td><td class="right num"><b>${money(x.total)}</b></td></tr>`; }).join("")}
      <tr><td><b>Total</b></td><td></td><td></td><td class="right num"><b>${money(b.allocated_total)}</b></td></tr></table>
    </section>
    <section class="card stack">
      ${locked()
        ? `<p>Finalised ${when(S.dinner.finalised_at)}. Guests can see their amount and your payment details.</p><button class="block" data-reopen>Reopen the bill</button>`
        : `<button class="primary block" data-finalise ${b.accounted_for ? "" : "disabled"}>Finalise and send payment details</button>
           <p class="muted">${b.accounted_for ? "Locks the bill and shows everyone what to pay you." : "Available once everything is accounted for."}</p>`}
    </section>`;
  $("#total")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const c = $("#tot").value.trim() ? toCents($("#tot").value) : null;
    if ($("#tot").value.trim() && c == null) { toast("Total like 188.10", true); return; }
    try { await call("POST", `${oBase}/total`, { version: S.dinner.version, cents: c, source: "quoted" }); refresh(); } catch (err) { fail(err); refresh(); }
  });
}

function billLine(l) {
  const c = lineCalc(l);
  const open = openEditors.has(l.id);
  const who = l.split_mode === "proportional"
    ? (l.allocations.length ? `Proportional among ${l.allocations.map((a) => esc(nameOf(a.participant_id))).join(", ")}` : "Proportional across everyone")
    : Object.entries(c.shares).map(([pid, cents]) => `${esc(nameOf(pid))} ${money(cents)}`).join(" · ");
  return `<div class="line">
    <div class="grow">
      <b>${esc(l.name)}</b>${l.variant_label ? ` · ${esc(l.variant_label)}` : ""}${l.quantity > 1 ? ` ×${l.quantity}` : ""}
      ${l.kind !== "item" ? `<span class="state info">${esc(l.kind)}</span>` : ""}${l.source === "receipt" ? ' <span class="state">from receipt</span>' : ""}
      ${l.total_override_cents != null ? ' <span class="state warn">price set to receipt</span>' : ""}
      <div class="who">${SPLIT_LABEL[l.split_mode]} · ${who || ""}${c.unallocated ? ` <b style="color:var(--bad)">${money(c.unallocated)} unallocated</b>` : ""}</div>
      ${l.note ? `<div class="who">“${esc(l.note)}”</div>` : ""}
      ${open ? allocEditor(l) : ""}
    </div>
    <div class="stack right" style="flex:none"><b class="num">${money(c.amount)}</b>${locked() ? "" : `<button class="small" data-editline="${l.id}">${open ? "Close" : "Edit"}</button>`}</div>
  </div>`;
}

function allocEditor(l) {
  const weights = Object.fromEntries(l.allocations.map((a) => [a.participant_id, a.weight]));
  const modes = l.kind === "item" ? ["equal", "shares", "units"] : ["proportional", "equal", "shares"];
  return `<form class="editor stack" data-keep data-lineform="${l.id}">
    <div><label>Split</label><select name="mode">${modes.map((m) => `<option value="${m}" ${l.split_mode === m ? "selected" : ""}>${SPLIT_LABEL[m]}</option>`).join("")}</select></div>
    <p class="muted">Tick who's in. For unequal shares, the numbers are parts (2 and 1 = two-thirds and one-third). Per unit: how many each had. Proportional with nobody ticked = everyone.</p>
    ${S.participants.map((p) => `<div class="row"><label class="check grow"><input type="checkbox" name="who" value="${p.id}" ${p.id in weights ? "checked" : ""}> ${esc(p.name)}</label>
      <input name="w_${p.id}" type="number" min="1" max="999" value="${weights[p.id] || 1}" style="width:5rem" aria-label="Parts for ${esc(p.name)}"></div>`).join("")}
    <div class="field-row">
      <div><label>Quantity</label><input name="qty" type="number" min="1" max="99" value="${l.quantity}"></div>
      <div><label>Line total override</label><input name="total" inputmode="decimal" value="${centsToInput(l.total_override_cents)}" placeholder="${money(lineCalc(l).amount)}"></div>
    </div>
    ${l.menu_item_id ? "" : `<div><label>Name</label><input name="name" value="${esc(l.name)}"></div>`}
    <div class="row"><button class="primary grow">Save</button><button type="button" class="danger small" data-delline="${l.id}">Remove</button></div>
  </form>`;
}

async function saveLine(form) {
  const l = S.lines.find((x) => x.id === form.dataset.lineform);
  const f = new FormData(form);
  const mode = f.get("mode");
  const allocations = f.getAll("who").map((pid) => ({ participant_id: pid, weight: parseInt(f.get(`w_${pid}`), 10) || 1 }));
  const totalText = String(f.get("total") || "").trim();
  const total = totalText ? toCents(totalText) : null;
  if (totalText && total == null) { toast("Amount like 12.50", true); return; }
  try {
    let version = l.version;
    const patch = {};
    if (parseInt(f.get("qty"), 10) !== l.quantity) patch.quantity = parseInt(f.get("qty"), 10);
    if (total !== l.total_override_cents) patch.total_cents = total;
    if (f.get("name") && f.get("name") !== l.name) patch.name = f.get("name");
    if (Object.keys(patch).length) {
      const r = await call("PATCH", `${gBase()}/lines/${l.id}`, { version, ...patch });
      version = r.line.version;
    }
    await call("PUT", `${gBase()}/lines/${l.id}/allocations`, { version, allocations, split_mode: mode });
    openEditors.delete(l.id);
    toast("Saved");
  } catch (err) { fail(err); }
  refresh();
}

function addItemSheet() {
  const items = S.menu.items;
  const s = sheet(`
    <div class="row between"><h2>Add an item</h2><button class="small" data-close>Cancel</button></div>
    <form id="ai" class="stack">
      <div><label>From the menu</label><select name="menu"><option value="">— typed in below —</option>
        ${items.map((i) => `<option value="${i.id}">${esc(i.name)}${i.variants.length ? "" : " " + money(i.price_cents)}</option>`).join("")}</select></div>
      <div id="vwrap"></div>
      <div class="field-row"><div><label>Or name</label><input name="name"></div><div><label>Price each</label><input name="price" inputmode="decimal"></div></div>
      <div><label>Quantity</label><input name="qty" type="number" min="1" max="99" value="1"></div>
      <fieldset style="border:0;padding:0;margin:0"><legend class="muted">Who had it (equal split)</legend>
        ${S.participants.map((p) => `<label class="check"><input type="checkbox" name="who" value="${p.id}"> ${esc(p.name)}</label>`).join("")}</fieldset>
      <button class="primary block">Add</button>
    </form>`);
  const form = $("#ai", s.el);
  form.menu.addEventListener("change", () => {
    const it = items.find((i) => i.id === form.menu.value);
    $("#vwrap", s.el).innerHTML = it?.variants.length ? `<label>Size</label><select name="variant">${it.variants.map((v) => `<option>${esc(v.label)}</option>`).join("")}</select>` : "";
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const who = $$("input[name=who]:checked", form).map((x) => x.value);
    const body = { quantity: parseInt(form.qty.value, 10) || 1, participants: who, shared: who.length > 1, separate: true };
    if (form.menu.value) { body.menu_item_id = form.menu.value; body.variant_label = form.variant?.value || ""; if (form.price.value) body.unit_price_cents = toCents(form.price.value); }
    else { body.name = form.name.value; body.unit_price_cents = toCents(form.price.value); if (!body.name || body.unit_price_cents == null) { toast("Name and price, please", true); return; } }
    try { await call("POST", `${gBase()}/lines`, body); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

function adjustSheet(suggested) {
  const s = sheet(`
    <div class="row between"><h2>Surcharge, discount or adjustment</h2><button class="small" data-close>Cancel</button></div>
    <form id="adj" class="stack">
      <div><label>What is it? (guests see this)</label><input name="name" required placeholder="e.g. Sunday surcharge 10%, Unexplained difference"></div>
      <div class="field-row"><div><label>Type</label><select name="kind"><option value="surcharge">Surcharge</option><option value="discount">Discount</option><option value="adjustment" ${suggested ? "selected" : ""}>Adjustment</option></select></div>
        <div><label>Amount (negative for a discount)</label><input name="amount" inputmode="decimal" required value="${suggested ? (suggested / 100).toFixed(2) : ""}"></div></div>
      <div><label>Split</label><select name="mode"><option value="proportional">In proportion to each person's items</option><option value="equal">Equally among ticked people</option></select></div>
      <fieldset style="border:0;padding:0;margin:0"><legend class="muted">People (none ticked = everyone with items)</legend>
        ${S.participants.map((p) => `<label class="check"><input type="checkbox" name="who" value="${p.id}"> ${esc(p.name)}</label>`).join("")}</fieldset>
      <p class="muted">Differences are never spread silently — this adds a visible line everyone can see.</p>
      <button class="primary block">Add</button>
    </form>`);
  const form = $("#adj", s.el);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const cents = toCents(form.amount.value);
    if (cents == null || cents === 0) { toast("Enter an amount like 17.10", true); return; }
    let who = $$("input[name=who]:checked", form).map((x) => x.value);
    if (form.mode.value === "equal" && !who.length) who = S.participants.map((p) => p.id);
    try {
      await call("POST", `${gBase()}/lines`, { name: form.name.value, kind: form.kind.value, unit_price_cents: cents, total_cents: cents,
        split_mode: form.mode.value, participants: who, shared: true });
      s.close(); refresh();
    } catch (err) { fail(err); }
  });
}

// ----------------------------------------------------------- receipt
const RSTATE = { pending: ["Needs review", "warn"], matched: ["Matched", "good"], added: ["Added as new item", "info"], ignored: ["Ignored", ""] };

function paneReceipt() {
  const r = S.receipt;
  const lineName = (id) => { const l = S.lines.find((x) => x.id === id); return l ? `${l.name}${l.variant_label ? " · " + l.variant_label : ""} ×${l.quantity} (${money(lineCalc(l).amount)}, ${l.allocations.map((a) => nameOf(a.participant_id)).join(", ") || "nobody"})` : "(removed)"; };
  const used = new Set();
  if (r) r.lines.forEach((x) => { (x.matched_line_ids || []).forEach((i) => used.add(i)); if (x.added_line_id) used.add(x.added_line_id); (x.state === "pending" ? x.suggestion?.line_ids || [] : []).forEach((i) => used.add(i)); });
  const missing = r && r.status === "review" ? S.lines.filter((l) => l.kind === "item" && !used.has(l.id)) : [];
  $("#pane").innerHTML = `
    <section class="card stack">
      <h2>Itemised receipt</h2>
      <p class="muted">Menu-first: scan it at the end to check against what was recorded. Receipt-first: scan it, add the lines, and let guests claim them.</p>
      ${locked() ? '<p class="muted">Reopen the bill to scan another receipt.</p>' : `
      <form id="rc" class="stack"><p class="muted">Long receipt? Photograph it in parts, top to bottom.</p><div data-tray="receipt"></div><button class="primary" id="rbtn">Scan receipt</button></form>
      ${S.demo_tools ? '<button data-demoreceipt>Use the sample receipt (demo)</button>' : ""}`}
    </section>
    ${!r ? "" : r.status === "processing" ? `<div class="banner info"><span class="spinner"></span> Reading the receipt…</div>`
      : r.status === "failed" ? `<div class="banner bad">${esc(r.error)} You can still enter the restaurant's total on the Bill tab.</div>` : `
    <section class="card">
      <div class="row">${r.images.map((im) => `<a href="${gBase()}/image/${esc(im)}" target="_blank" rel="noopener"><img class="thumb" src="${gBase()}/image/${esc(im)}" alt="Receipt"></a>`).join("")}
        <div class="grow"><dl class="stat"><dt>Receipt total</dt><dd>${money(r.total_cents)}</dd>${r.gst_cents != null ? `<dt>GST ${r.gst_included ? "(already included)" : ""}</dt><dd>${money(r.gst_cents)}</dd>` : ""}<dt>Recorded now</dt><dd>${money(S.bill.recorded_total)}</dd></dl></div></div>
      ${r.flags.map((f) => `<div class="banner warn">⚠ ${esc(f)}</div>`).join("")}
      ${locked() ? "" : `<div class="row" style="margin-top:.5rem"><button class="small" data-addall>Add all unresolved lines as items</button><button class="small" data-resuggest>Suggest matches again</button><button class="small danger" data-discard>Discard receipt</button></div>`}
    </section>
    <section class="card">
      <h2>Receipt lines</h2>
      ${r.lines.map((x) => {
        const [label, tone] = RSTATE[x.state];
        const sug = x.suggestion || {};
        return `<div class="line"><div class="grow">
          <b>${esc(x.description)}</b> <span class="muted">${x.kind !== "item" ? esc(x.kind.replace("_", " ")) : ""}</span>
          <div><span class="state ${tone}">${label}</span>${sug.auto && x.state === "matched" ? ' <span class="muted">exact match</span>' : ""}</div>
          ${x.flags.map((f) => `<div class="banner warn" style="margin:.3rem 0">⚠ ${esc(f)}</div>`).join("")}
          ${x.state === "matched" ? `<div class="who">↔ ${x.matched_line_ids.map((i) => esc(lineName(i))).join("<br>↔ ")}</div>` : ""}
          ${x.state === "pending" && sug.line_ids?.length ? `<div class="who">Suggested: ${sug.line_ids.map((i) => esc(lineName(i))).join(", ")}</div>${(sug.reasons || []).map((m) => `<div class="who">• ${esc(m)}</div>`).join("")}` : ""}
          ${x.state === "pending" && !sug.line_ids?.length && x.kind === "item" ? '<div class="who"><b>Not recorded by anyone</b> — add it so someone can claim it, or match it by hand.</div>' : ""}
          ${locked() ? "" : `<div class="row" style="margin-top:.35rem">${x.state === "pending" ? `
            ${x.kind === "item" && sug.line_ids?.length ? `<button class="small primary" data-raccept="${x.id}">Accept suggestion</button>` : ""}
            ${x.kind === "item" ? `<button class="small" data-rmatch="${x.id}">Match…</button>` : ""}
            <button class="small" data-radd="${x.id}">Add as new ${x.kind === "item" ? "item" : x.kind}</button>
            <button class="small" data-rignore="${x.id}">Ignore</button><button class="small" data-redit="${x.id}">Fix amount</button>`
            : `<button class="small" data-rreset="${x.id}">Undo</button>`}</div>`}
        </div><div class="num"><b>${money(x.line_total_cents)}</b>${x.quantity > 1 ? `<div class="muted">×${x.quantity}</div>` : ""}</div></div>`;
      }).join("")}
    </section>
    ${missing.length ? `<section class="card"><h2>Recorded but not on the receipt</h2>
      ${missing.map((l) => `<div class="line"><div class="grow"><b>${esc(l.name)}</b> ×${l.quantity} <span class="muted">${l.allocations.map((a) => esc(nameOf(a.participant_id))).join(", ")}</span>
        ${l.not_on_receipt_ok ? '<div class="who">Kept on purpose</div>' : ""}</div>
        <div class="num">${money(lineCalc(l).amount)}</div>
        ${locked() ? "" : `<div class="stack">${l.not_on_receipt_ok ? "" : `<button class="small" data-keep-line="${l.id}">Keep</button>`}<button class="small danger" data-dropline="${l.id}">Remove</button></div>`}</div>`).join("")}
      <p class="muted">Maybe it wasn't served, was on the house, or is on the receipt under another name (use Match… on that line).</p></section>` : ""}`}`;
  wireTray("receipt", (n) => { $("#rbtn").disabled = !n; });
  $("#rc")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!trays.receipt.length) { toast("Take or choose a photo of the receipt.", true); return; }
    const fd = new FormData();
    for (const p of trays.receipt) fd.append("files", p.file);
    $("#rbtn").disabled = true;
    try { await call("POST", `${oBase}/receipt`, undefined, { form: fd }); emptyTray("receipt"); } catch (err) { fail(err); }
    refresh();
  });
}

function matchSheet(x) {
  const sug = new Set(x.suggestion?.line_ids || []);
  const r = S.receipt;
  const taken = new Set(r.lines.filter((o) => o.id !== x.id).flatMap((o) => [...(o.matched_line_ids || []), o.added_line_id].filter(Boolean)));
  const candidates = S.lines.filter((l) => l.kind === "item" && !taken.has(l.id));
  const s = sheet(`
    <div class="row between"><h2>Match “${esc(x.description)}”</h2><button class="small" data-close>Cancel</button></div>
    <p class="muted">Receipt: ${x.quantity} for ${money(x.line_total_cents)}. Tick the recorded item(s) this is. Who's sharing them stays as it is — nothing is added twice.</p>
    <form id="mf" class="stack">
      ${candidates.map((l) => `<label class="check"><input type="checkbox" name="l" value="${l.id}" ${sug.has(l.id) ? "checked" : ""}> ${esc(l.name)}${l.variant_label ? " · " + esc(l.variant_label) : ""} ×${l.quantity} — ${money(lineCalc(l).amount)} <span class="muted">(${l.allocations.map((a) => esc(nameOf(a.participant_id))).join(", ") || "nobody"})</span></label>`).join("") || '<p class="muted">No recorded items left to match.</p>'}
      <label class="check"><input type="checkbox" name="use" checked> Use the receipt's quantity and price if they differ</label>
      <button class="primary block">Confirm match</button>
    </form>`);
  $("#mf", s.el).addEventListener("submit", async (e) => {
    e.preventDefault();
    const ids = $$("input[name=l]:checked", s.el).map((i) => i.value);
    try { await call("POST", `${oBase}/receipt/lines/${x.id}/match`, { line_ids: ids, use_receipt: $("input[name=use]", s.el).checked }); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

function fixReceiptSheet(x) {
  const s = sheet(`
    <div class="row between"><h2>Fix receipt line</h2><button class="small" data-close>Cancel</button></div>
    <form id="fx" class="stack">
      <div><label>Description</label><input name="d" value="${esc(x.description)}"></div>
      <div class="field-row"><div><label>Quantity</label><input name="q" type="number" min="1" value="${x.quantity}"></div>
      <div><label>Line total</label><input name="t" inputmode="decimal" value="${centsToInput(x.line_total_cents)}"></div></div>
      <div><label>Type</label><select name="k">${["item", "surcharge", "discount", "tip", "tax_info", "subtotal", "total", "payment", "other"].map((k) => `<option ${x.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select></div>
      <button class="primary block">Save</button>
    </form>`);
  $("#fx", s.el).addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const t = f.t.value.trim() ? toCents(f.t.value) : null;
    try { await call("POST", `${oBase}/receipt/lines/${x.id}/edit`, { description: f.d.value, quantity: parseInt(f.q.value, 10), line_total_cents: t, kind: f.k.value }); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

// ---------------------------------------------------------- payments
const PSTATE = {
  awaiting: ["Awaiting payment", "warn"], marked_sent: ["Guest says sent", "info"], part_paid: ["Part paid", "warn"],
  confirmed: ["Received", "good"], overpaid: ["Overpaid", "info"], nothing_owed: ["Nothing owed", "good"], self: ["You (paid restaurant)", "good"], not_ready: ["Not finalised", ""],
};

function panePay() {
  const st = S.payment_statuses;
  const live = S.payments.filter((p) => !p.undone_at);
  $("#pane").innerHTML = `
    ${!S.dinner.instructions_issued_at ? '<div class="banner info">Payment details go to guests when you finalise the bill.</div>' : ""}
    <section class="card">
      <h2>Who's paid</h2>
      <table class="plain"><tr><th>Person</th><th class="right">Share</th><th class="right">Received</th><th class="right">Balance</th></tr>
      ${S.participants.map((p) => { const x = st[p.id]; const [l, t] = PSTATE[x.state] || [x.state, ""]; return `<tr>
        <td><b>${esc(p.name)}</b><div class="muted">${esc(p.reference)}</div><span class="state ${t}">${l}</span></td>
        <td class="right num">${money(x.share)}</td><td class="right num">${money(x.received)}</td>
        <td class="right num"><b>${p.is_organiser ? "—" : money(x.balance)}</b>${!p.is_organiser && S.dinner.instructions_issued_at ? `<div><button class="small" data-manualpay="${p.id}">Record payment</button></div>` : ""}</td></tr>`; }).join("")}
      </table>
    </section>
    ${S.bank_review.length ? `<section class="card"><h2>Incoming transfers to check</h2>
      ${S.bank_review.map((t) => `<div class="line"><div class="grow">
        <b class="num">${money(t.amount_cents)}</b> · ${when(t.created_at)} ${t.is_simulated ? '<span class="state">simulated</span>' : ""} <span class="state ${t.match_state === "review" ? "warn" : ""}">${t.match_state === "review" ? "Needs review" : "No match"}</span>
        <div class="who">${t.message ? `Message: “${esc(t.message)}” · ` : ""}${esc(t.description)}${t.raw_text ? ` · ${esc(t.raw_text)}` : ""}</div>
        <div class="who">${esc(t.match_note)}</div>
        ${(t.candidates || []).map((c) => `<div class="editor"><b>${esc(c.name)}</b> (${esc(c.reference)}) owes ${money(c.outstanding)}<div class="who">${c.reasons.map(esc).join(" · ")}</div>
          <button class="small primary" data-txconfirm="${t.id}" data-pid="${c.participant_id}">Confirm from ${esc(c.name)}</button></div>`).join("")}
        <div class="row" style="margin-top:.35rem"><select data-txpick="${t.id}" style="width:auto"><option value="">Someone else…</option>${S.participants.filter((p) => !p.is_organiser).map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>
          <button class="small" data-txignore="${t.id}">Not a dinner payment</button></div>
      </div></div>`).join("")}</section>` : ""}
    <section class="card">
      <h2>Payment history</h2>
      ${S.payments.length ? S.payments.map((p) => `<div class="line ${p.undone_at ? "pending" : ""}"><div class="grow">
        <b>${esc(nameOf(p.participant_id))}</b> · ${money(p.amount_cents)} · ${when(p.created_at)}
        <div class="who">${esc({ up_auto: "Matched automatically", up_review: "Up transfer, confirmed by you", manual: "Recorded by hand", simulated: "Simulated" }[p.source] || p.source)} — ${esc(p.match_reason)}</div>
        ${p.undone_at ? `<div class="who">Undone ${when(p.undone_at)}: ${esc(p.undo_reason)}</div>` : ""}</div>
        ${p.undone_at ? "" : `<button class="small" data-undopay="${p.id}">Undo</button>`}</div>`).join("") : '<p class="muted">Nothing received yet.</p>'}
      <p class="muted">${live.length} payment(s) totalling ${money(live.reduce((a, p) => a + p.amount_cents, 0))}. Undone payments stay listed for the record.</p>
    </section>
    ${S.demo_tools && S.dinner.instructions_issued_at ? `<section class="card stack"><h2>Simulate a transfer (demo)</h2>
      <p class="muted">Runs the real matching rules on a pretend transfer. Try a wrong amount, no reference, or two people owing the same.</p>
      <form id="sim" data-keep class="stack">
        <div><label>From</label><select name="p">${S.participants.filter((p) => !p.is_organiser).map((p) => `<option value="${p.id}">${esc(p.name)} (${esc(p.reference)})</option>`).join("")}</select></div>
        <div class="field-row"><div><label>Amount</label><input name="a" inputmode="decimal"></div><div><label>Message / reference</label><input name="m"></div></div>
        <div><label>Sender text (Up's description)</label><input name="d" placeholder="e.g. Helen Smith"></div>
        <button class="primary">Send pretend transfer</button>
      </form></section>` : ""}`;
  const sim = $("#sim");
  if (sim) {
    const fill = () => { const p = S.participants.find((x) => x.id === sim.p.value); const x = st[p.id]; sim.a.value = centsToInput(Math.max(x.balance, 0)); sim.m.value = p.reference; sim.d.value = p.name; };
    sim.p.addEventListener("change", fill);
    fill();
    sim.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const r = await call("POST", `${oBase}/simulate-payment`, { amount_cents: toCents(sim.a.value), message: sim.m.value, description: sim.d.value });
        toast(`${r.match_state === "auto" ? "Matched automatically" : r.match_state === "review" ? "Sent to review" : "No match"}: ${r.note}`);
        refresh();
      } catch (err) { fail(err); }
    });
  }
}

function paneLog() {
  $("#pane").innerHTML = `<section class="card"><h2>Activity</h2>${S.audit.map((a) => `<div class="line"><div class="grow">${esc(a.message)}<div class="who">${esc(a.actor)} · ${when(a.at)}</div></div></div>`).join("")}</section>`;
}

// ------------------------------------------------------------ actions
async function finaliseFlow() {
  try {
    await call("POST", `${oBase}/finalise`, { revision: S.dinner.revision });
    toast("Finalised — guests can now see what to pay");
    dtab = "pay";
  } catch (err) { fail(err); }
  refresh();
}

async function reopenFlow() {
  try {
    await call("POST", `${oBase}/reopen`, { version: S.dinner.version, acknowledged: false });
  } catch (err) {
    if (err instanceof ApiError && err.detail?.code === "confirm_reopen") {
      const s = sheet(`<h2>Reopen the bill?</h2><div class="banner warn">${esc(err.detail.message)}</div>
        <p>Guests will see a notice not to pay until it's final again.</p>
        <div class="row"><button class="grow" data-close>Keep it final</button><button class="primary grow" data-go>Reopen</button></div>`);
      $("[data-go]", s.el).addEventListener("click", async () => {
        try { await call("POST", `${oBase}/reopen`, { version: S.dinner.version, acknowledged: true }); s.close(); refresh(); } catch (e2) { fail(e2); }
      });
      return;
    }
    fail(err);
  }
  refresh();
}

document.addEventListener("submit", (e) => {
  const f = e.target;
  if (f.dataset.itemform) { e.preventDefault(); saveItem(f); }
  if (f.dataset.lineform) { e.preventDefault(); saveLine(f); }
});

document.addEventListener("change", async (e) => {
  const pick = e.target.closest("[data-txpick]");
  if (pick && pick.value) {
    try { await call("POST", `/api/o/tx/${pick.dataset.txpick}/confirm`, { participant_id: pick.value }); toast("Payment confirmed"); } catch (err) { fail(err); }
    refresh();
  }
});

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button,[data-copy]");
  if (!t || !S) return;
  const d = t.dataset;
  const act = async (fn) => { try { await fn(); } catch (err) { fail(err); } refresh(); };
  if (d.dtab) { dtab = d.dtab; try { sessionStorage.setItem(`dt_otab_${dinnerId}`, dtab); } catch { /* fine */ } renderDinner(); window.scrollTo(0, 0); }
  else if (d.copy !== undefined) copy(d.copy, "Link");
  else if (d.page) act(() => call("POST", `${oBase}/pages/${d.page}/${d.act}`));
  else if (d.dropunseen !== undefined) act(() => call("POST", `${oBase}/menu/drop-unseen`));
  else if (d.delpage) { if (confirmSheet("Delete this page and its dishes? Dishes already ordered are kept (hidden from the menu).", () => act(() => call("DELETE", `${oBase}/pages/${d.delpage}`)))) return; }
  else if (d.edititem) { openEditors.has(d.edititem) ? openEditors.delete(d.edititem) : openEditors.add(d.edititem); renderDinner(); }
  else if (d.avail) { const i = S.menu.items.find((x) => x.id === d.avail); act(() => call("PATCH", `${oBase}/items/${i.id}`, { version: i.version, unavailable: !i.unavailable })); }
  else if (d.delitem) confirmSheet("Delete this dish?", () => act(() => call("DELETE", `${oBase}/items/${d.delitem}`)));
  else if (d.editline) { openEditors.has(d.editline) ? openEditors.delete(d.editline) : openEditors.add(d.editline); renderDinner(); }
  else if (d.delline) { const l = S.lines.find((x) => x.id === d.delline); confirmSheet(`Remove ${l.name} from the bill?`, () => act(() => call("DELETE", `${gBase()}/lines/${l.id}?version=${l.version}`))); }
  else if (d.additem !== undefined) addItemSheet();
  else if (d.adjust !== undefined) adjustSheet(d.adjust ? parseInt(d.adjust, 10) : 0);
  else if (d.finalise !== undefined) finaliseFlow();
  else if (d.reopen !== undefined) reopenFlow();
  else if (d.demoreceipt !== undefined) act(() => call("POST", `${oBase}/demo-receipt`));
  else if (d.addall !== undefined) act(() => call("POST", `${oBase}/receipt/add-all`));
  else if (d.resuggest !== undefined) act(() => call("POST", `${oBase}/receipt/resuggest`));
  else if (d.discard !== undefined) confirmSheet("Discard this receipt? Items already added from it stay on the bill.", () => act(() => call("POST", `${oBase}/receipt/discard`)));
  else if (d.rmatch) matchSheet(S.receipt.lines.find((x) => x.id === d.rmatch));
  else if (d.raccept) {
    // Take the suggestion as shown; receipt quantity/price win where they differ.
    const x = S.receipt.lines.find((r) => r.id === d.raccept);
    act(() => call("POST", `${oBase}/receipt/lines/${x.id}/match`, { line_ids: x.suggestion.line_ids, use_receipt: true }));
  }
  else if (d.radd) act(() => call("POST", `${oBase}/receipt/lines/${d.radd}/add`));
  else if (d.rignore) act(() => call("POST", `${oBase}/receipt/lines/${d.rignore}/ignore`));
  else if (d.rreset) act(() => call("POST", `${oBase}/receipt/lines/${d.rreset}/reset`));
  else if (d.redit) fixReceiptSheet(S.receipt.lines.find((x) => x.id === d.redit));
  else if (d.keepLine) { const l = S.lines.find((x) => x.id === d.keepLine); act(() => call("PATCH", `${gBase()}/lines/${l.id}`, { version: l.version, not_on_receipt_ok: true })); }
  else if (d.dropline) { const l = S.lines.find((x) => x.id === d.dropline); confirmSheet(`Remove ${l.name}? Anyone sharing it stops paying for it.`, () => act(() => call("DELETE", `${gBase()}/lines/${l.id}?version=${l.version}`))); }
  else if (d.manualpay) manualPaySheet(d.manualpay);
  else if (d.undopay) undoSheet(d.undopay);
  else if (d.txconfirm) act(() => call("POST", `/api/o/tx/${d.txconfirm}/confirm`, { participant_id: d.pid }));
  else if (d.txignore) act(() => call("POST", `/api/o/tx/${d.txignore}/ignore`));
  else if (d.plink) {
    try { const r = await call("POST", `${oBase}/people/${d.plink}/link`); sheet(`<h2>Personal link for ${esc(nameOf(d.plink))}</h2><p class="muted">Works once, for 12 hours. Send it only to them — it signs them in as themselves.</p><p><code style="word-break:break-all">${esc(r.url)}</code></p><div class="row"><button class="grow" data-close>Done</button><button class="primary grow" data-copy="${esc(r.url)}">Copy</button></div>`); } catch (err) { fail(err); }
  } else if (d.prename) renameSheet(d.prename);
  else if (d.cleardinner !== undefined) clearDinnerSheet();
  else if (d.premove) confirmSheet(`Remove ${nameOf(d.premove)}?`, () => act(() => call("DELETE", `${oBase}/people/${d.premove}`)));
});

function confirmSheet(message, onYes) {
  const s = sheet(`<p>${esc(message)}</p><div class="row"><button class="grow" data-close>Cancel</button><button class="primary grow" data-go>Yes</button></div>`);
  $("[data-go]", s.el).addEventListener("click", () => { s.close(); onYes(); });
  return true;
}

function clearDinnerSheet() {
  const owing = S.dinner.status === "finalised"
    ? Object.values(S.payment_statuses).filter((x) => ["awaiting", "marked_sent", "part_paid"].includes(x.state)).length : 0;
  const warn = owing
    ? `<div class="banner warn">${owing} ${owing > 1 ? "people still owe" : "person still owes"} you. Once it's cleared, their transfers won't be matched automatically.</div>`
    : S.participants.length > 1 ? `<div class="banner warn">The guest link stops working once it's cleared.</div>` : "";
  const s = sheet(`<h2>Clear ${esc(S.dinner.restaurant_name || "this dinner")}?</h2>${warn}
    <p class="muted">It disappears from your list. You can bring it back later from “Cleared dinners”.</p>
    <div class="row"><button class="grow" data-close>Keep it</button><button class="primary grow" data-go>Clear it</button></div>`);
  $("[data-go]", s.el).addEventListener("click", async () => {
    try { await call("PATCH", oBase, { archived: true }); location.href = "/o"; } catch (err) { fail(err); }
  });
}

function renameSheet(pid) {
  const s = sheet(`<h2>Rename</h2><form id="rnf" class="stack"><input name="n" value="${esc(nameOf(pid))}" maxlength="40" required>
    <p class="muted">Their payment reference won't change.</p><button class="primary block">Save</button></form>`);
  $("#rnf", s.el).addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await call("PATCH", `${oBase}/people/${pid}`, { name: e.target.n.value }); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

function manualPaySheet(pid) {
  const x = S.payment_statuses[pid];
  const s = sheet(`<h2>Record a payment from ${esc(nameOf(pid))}</h2>
    <form id="mp" class="stack"><div><label>Amount received</label><input name="a" inputmode="decimal" value="${centsToInput(Math.max(x.balance, 0))}" required></div>
    <div><label>Note</label><input name="n" placeholder="e.g. cash, or seen in my banking app"></div>
    <p class="muted">Use a negative amount to record a refund you gave back.</p>
    <button class="primary block">Mark received</button></form>`);
  $("#mp", s.el).addEventListener("submit", async (e) => {
    e.preventDefault();
    const c = toCents(e.target.a.value);
    if (c == null || c === 0) { toast("Amount like 23.50", true); return; }
    try { await call("POST", `${oBase}/payments`, { participant_id: pid, amount_cents: c, note: e.target.n.value }); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

function undoSheet(payId) {
  const s = sheet(`<h2>Undo this payment?</h2><form id="uf" class="stack"><div><label>Why (kept in the history)</label><input name="r" placeholder="e.g. matched to the wrong person"></div>
    <p class="muted">If it came from Up, the transfer goes back to “needs review” so you can assign it correctly.</p><button class="primary block">Undo</button></form>`);
  $("#uf", s.el).addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await call("POST", `/api/o/payments/${payId}/undo`, { reason: e.target.r.value }); s.close(); refresh(); } catch (err) { fail(err); }
  });
}

// --------------------------------------------------------------- boot
function setConn(ok) {
  const el = $("#conn");
  el.classList.remove("hidden");
  // Only worth a word when something's wrong.
  el.className = ok ? "conn hidden" : "conn off";
  el.textContent = ok ? "" : "Reconnecting";
}

(async () => {
  try {
    if (view === "home") await home();
    else if (view === "settings") await settings();
    else if (view === "dinner") {
      await refresh();
      live(`/api/d/${S.public_token}/events`, { onRevision: refreshSoon, onStatus: setConn });
    }
  } catch (e) {
    if (e.status !== 401) app.innerHTML = `<div class="wrap"><div class="banner bad">${esc(explainError(e))}</div></div>`;
  }
})();
