// The guest webpage (also the organiser's own "diner view").
import { $, $$, api, copy, esc, explainError, live, money, Outbox, prefsControls, sheet, toast, toCents, when } from "./core.js";
import { splitCents } from "./split.js";

const token = document.body.dataset.token;
const base = `/api/d/${token}`;
const app = $("#app");
let S = null;
let online = true;
let tab = "menu";
try { tab = sessionStorage.getItem(`dt_tab_${token}`) || "menu"; } catch { /* default */ }
const ui = { q: "", filters: new Set(), maxPrice: "", open: new Set() };

const outbox = new Outbox(`dt_outbox_${token}`, {
  onChange: renderConn,
  onDone: (op, result) => {
    if (result?.exists) chooseJoinOrSeparate(op, result.exists);
    else if (op.done) toast(op.done);
    refreshSoon();
  },
  onRejected: (op, e) => { toast(`${op.label ? op.label + ": " : ""}${explainError(e)}`, true); refreshSoon(); },
});

// ------------------------------------------------------------------ data
let refreshTimer;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refresh, 120); }
async function refresh() {
  try {
    S = await api("GET", `${base}/state`);
    online = true;
    render();
  } catch (e) {
    online = false;
    renderConn();
    if (!S) app.innerHTML = `<div class="wrap"><div class="banner bad">${esc(explainError(e))}</div></div>`;
  }
}

const people = () => Object.fromEntries(S.participants.map((p) => [p.id, p]));
const nameOf = (id) => people()[id]?.name || "Someone";
const me = () => S.me?.id;
const lineCalc = (l) => S.bill.lines[l.id] || { amount: 0, shares: {}, unallocated: 0 };
const itemById = (id) => S.menu.items.find((i) => i.id === id);
const catById = (id) => S.menu.categories.find((c) => c.id === id);
const locked = () => S.dinner.status === "finalised";

// ------------------------------------------------------------------ shell
function renderConn() {
  const el = $("#conn");
  if (!el) return;
  const pending = outbox.size;
  if (!online || outbox.failing) {
    el.className = "conn off";
    el.textContent = pending ? `Offline · ${pending} waiting` : "Offline";
  } else if (pending) {
    el.className = "conn pending";
    el.textContent = `Sending ${pending}…`;
  } else {
    // Connected and up to date is the normal case: say nothing.
    el.className = "conn hidden";
    el.textContent = "";
  }
}

function render() {
  document.title = S.dinner.restaurant_name || "Dinner";
  $("#title").textContent = S.dinner.restaurant_name || "Dinner";
  // The organiser looking at the diner view needs a way back.
  const back = $("#orgback");
  if (back) { back.hidden = !S.organiser; back.href = `/o/d/${S.dinner.id}`; }
  if (!S.me) { renderJoin(); return; }
  document.body.classList.remove("no-tabs");
  const views = { menu: renderMenu, mine: renderMine, table: renderTable, pay: renderPay };
  (views[tab] || renderMenu)();
  renderTabs();
  renderConn();
}

function renderTabs() {
  const payDot = S.payment && S.payment.instructions && !["confirmed", "self", "nothing_owed"].includes(S.payment.state) ? '<span class="dot">!</span>' : "";
  const mine = S.lines.filter((l) => l.allocations.some((a) => a.participant_id === me())).length;
  $("#tabs").innerHTML = [
    ["menu", "☰", "Menu", ""],
    ["mine", "✓", "My order", mine ? `<span class="dot">${mine}</span>` : ""],
    ["table", "◎", "Table", ""],
    ["pay", "$", "Pay", payDot],
  ].map(([k, icon, label, dot]) => `<button role="tab" aria-selected="${tab === k}" data-tab="${k}"><b>${icon}</b>${label}${dot}</button>`).join("");
}

function setTab(k) {
  tab = k;
  try { sessionStorage.setItem(`dt_tab_${token}`, k); } catch { /* fine */ }
  window.scrollTo(0, 0);
  render();
}

// ------------------------------------------------------------------ join
function renderJoin() {
  document.body.classList.add("no-tabs");
  $("#tabs").innerHTML = "";
  app.innerHTML = `
    <div class="wrap hero stack">
      <h1>${esc(S.dinner.restaurant_name || "Dinner")}</h1>
      <p>See the menu, record what you order, and help sort out the bill.</p>
      <form id="join" class="card stack">
        <div><label for="name">Your name (shown to the table)</label>
        <input id="name" name="name" autocomplete="given-name" maxlength="40" required></div>
        <button class="primary block">Join the dinner</button>
        <p class="muted">No app or account needed. This phone remembers you for this dinner.
        If you switch phones, ask the organiser for a personal link.</p>
      </form>
    </div>`;
  $("#join").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    btn.disabled = true;
    try {
      await api("POST", `${base}/join`, { name: $("#name").value });
      await refresh();
    } catch (err) {
      toast(explainError(err), true);
      btn.disabled = false;
    }
  });
}

// ------------------------------------------------------------------ menu
const FILTERS = [
  ["vegetarian", "Vegetarian"],
  ["vegan_marked", "Vegan (menu)"],
  ["vegan_request", "Vegan on request"],
  ["vegan_possible", "Possibly vegan"],
  ["gluten_free", "Gluten free"],
  ["dairy_free", "Dairy free"],
  ["shortlist", "★ Shortlist"],
];

function priceRange(item) {
  const prices = item.variants.length ? item.variants.map((v) => v.price_cents).filter((p) => p != null) : [item.price_cents].filter((p) => p != null);
  return prices;
}

function priceLabel(item) {
  if (item.variants.length) return item.variants.map((v) => `${esc(v.label)} ${v.price_cents != null ? money(v.price_cents) : "?"}`).join(" · ");
  return item.price_cents != null ? money(item.price_cents) : '<span class="muted">No price</span>';
}

// The one-glance price for the list: a single price, or "from" the cheapest size.
function shortPrice(item) {
  if (!item.variants.length) return item.price_cents != null ? money(item.price_cents) : "";
  const p = priceRange(item);
  if (!p.length) return "";
  return item.variants.length > 1 ? `from ${money(Math.min(...p))}` : money(p[0]);
}

function passes(item) {
  const q = ui.q.trim().toLowerCase();
  if (q && !`${item.name} ${item.description}`.toLowerCase().includes(q)) return false;
  const f = ui.filters;
  const v = item.vegan?.status;
  const veganSet = ["vegan_marked", "vegan_request", "vegan_possible"].filter((k) => f.has(k));
  if (veganSet.length) {
    const ok = (f.has("vegan_marked") && v === "marked") || (f.has("vegan_request") && v === "on_request") || (f.has("vegan_possible") && v === "possible");
    if (!ok) return false;
  }
  if (f.has("vegetarian") && !(item.diet.includes("vegetarian") || v === "marked")) return false;
  if (f.has("gluten_free") && !item.diet.includes("gluten_free")) return false;
  if (f.has("dairy_free") && !(item.diet.includes("dairy_free") || v === "marked")) return false;
  if (f.has("shortlist") && !S.shortlist.includes(item.id)) return false;
  if (ui.maxPrice) {
    const cap = parseInt(ui.maxPrice, 10) * 100;
    const p = priceRange(item);
    if (!p.length || Math.min(...p) > cap) return false;
  }
  return true;
}

function badges(item) {
  const b = [];
  if (item.is_special) b.push('<span class="badge special">Special</span>');
  if (item.unavailable) b.push('<span class="badge off">Unavailable tonight</span>');
  const v = item.vegan?.status;
  if (v === "marked") b.push('<span class="badge vegan">Vegan (menu)</span>');
  if (v === "on_request") b.push('<span class="badge vegan">Vegan on request</span>');
  if (v === "possible") b.push('<span class="badge maybe">Possibly vegan — ask staff</span>');
  for (const l of item.labels) b.push(`<span class="badge">${esc(l)}</span>`);
  if (item.flags.length) b.push('<span class="badge flag">⚠ Check</span>');
  return b.length ? `<div class="badges">${b.join("")}</div>` : "";
}

function renderMenu() {
  const m = S.menu;
  const items = m.items.filter(passes);
  const cats = [...m.categories];
  const groups = cats.map((c) => ({ c, items: items.filter((i) => i.category_id === c.id) }));
  const loose = items.filter((i) => !i.category_id || !catById(i.category_id));
  if (loose.length) groups.push({ c: { id: "other", name: "More", note: "" }, items: loose });
  const shown = groups.filter((g) => g.items.length);

  const pending = m.pending_pages ? `<div class="banner info"><span class="spinner"></span> ${m.pending_pages} more menu page${m.pending_pages > 1 ? "s are" : " is"} being read — they'll appear here automatically.</div>` : "";
  const empty = !m.items.length ? `<div class="card center"><p><b>No menu yet.</b></p><p class="muted">The organiser is photographing the menu. You can still record items by hand.</p></div>` : "";
  const noMatch = m.items.length && !items.length ? `<p class="muted center">Nothing matches — try fewer filters.</p>` : "";
  const veganNote = ["vegan_marked", "vegan_request", "vegan_possible"].some((k) => ui.filters.has(k))
    ? `<div class="banner warn">“Vegan (menu)” is the restaurant's own label. “Possibly vegan” is our reading of the description only — confirm with staff. None of this is allergy advice.</div>` : "";

  const active = FILTERS.filter(([k]) => ui.filters.has(k));
  const nActive = active.length + (ui.maxPrice ? 1 : 0);

  app.innerHTML = `
    <div class="wrap">
      ${lockedBanner()}
      <div class="menu-tools">
        <div class="row" style="flex-wrap:nowrap">
          <input id="q" class="grow" type="search" placeholder="Search the menu" value="${esc(ui.q)}" aria-label="Search the menu">
          <button class="${nActive ? "primary" : ""}" data-filters>Filter${nActive ? ` · ${nActive}` : ""}</button>
        </div>
        ${nActive ? `<div class="filters" aria-label="Filters in use">
          ${active.map(([k, label]) => `<button class="chip" data-filter="${k}" aria-pressed="true" aria-label="Remove filter ${label}">${label} ✕</button>`).join("")}
          ${ui.maxPrice ? `<button class="chip" data-clearprice aria-pressed="true">Up to $${esc(ui.maxPrice)} ✕</button>` : ""}
        </div>` : ""}
        ${shown.length > 1 ? `<nav class="catnav" aria-label="Jump to a section"><span class="catnav-label">Jump to</span>${shown.map((g) => `<a href="#cat-${esc(g.c.id)}" data-jump="${esc(g.c.id)}">${esc(g.c.name)}</a>`).join("")}</nav>` : ""}
      </div>
      ${pending}${veganNote}${empty}${noMatch}
      ${shown.map((g) => `
        <h2 class="cat-head" id="cat-${g.c.id}">${esc(g.c.name)}</h2>
        ${g.c.note ? `<div class="cat-note">${esc(g.c.note)}</div>` : ""}
        ${g.c.extras?.length ? `<div class="cat-note">Add: ${g.c.extras.map((e) => `${esc(e.label)} ${e.price_cents != null ? money(e.price_cents) : ""}`).join(" · ")}</div>` : ""}
        <div class="card">${g.items.map(dishHtml).join("")}</div>`).join("")}
      ${m.legend.length ? `<p class="muted" style="margin-top:1rem">Menu key: ${uniqueLegend(m.legend).map((e) => `<b>${esc(e.symbol)}</b> ${esc(e.meaning)}`).join(" · ")}</p>` : ""}
      <section class="center"><button data-manual>+ Something not on the menu</button></section>
    </div>`;
  watchSections();
}

function uniqueLegend(list) {
  const seen = new Set();
  return list.filter((e) => !seen.has(e.symbol) && seen.add(e.symbol));
}

// Closed, a dish is just its name and price; tapping it opens everything else.
function dishHtml(item) {
  const open = ui.open.has(item.id);
  const starred = S.shortlist.includes(item.id);
  const extras = [...item.extras, ...(catById(item.category_id)?.extras || [])];
  const tags = [item.is_special ? '<span class="badge special">Special</span>' : "", item.unavailable ? '<span class="badge off">Unavailable</span>' : ""].join("");
  return `
    <div class="dish ${item.unavailable ? "unavailable" : ""} ${open ? "open" : ""}">
      <button class="dish-head" data-toggle="${item.id}" aria-expanded="${open}">
        <span class="grow dish-name">${starred ? '<span aria-label="shortlisted">★</span> ' : ""}${esc(item.name)} ${tags}</span>
        <span class="dish-price num">${shortPrice(item)}</span>
        <span class="chev" aria-hidden="true">›</span>
      </button>
      ${open ? `
        <div class="dish-body">
          ${item.description ? `<p class="dish-desc">${esc(item.description)}</p>` : ""}
          ${badges(item)}
          ${item.variants.length ? `<ul class="opts">${item.variants.map((v) => `<li><span>${esc(v.label)}</span><span class="num">${v.price_cents != null ? money(v.price_cents) : "?"}</span></li>`).join("")}</ul>` : ""}
          ${!item.variants.length && item.price_cents == null ? `<p class="muted">No price on the menu — ask staff.</p>` : ""}
          ${extras.length ? `<p class="muted">Extras:</p><ul class="opts">${extras.map((e) => `<li><span>+ ${esc(e.label)}</span><span class="num">${e.price_cents != null ? money(e.price_cents) : ""}</span></li>`).join("")}</ul>` : ""}
          ${item.vegan?.status === "possible" ? `<p class="banner warn">Possibly vegan: ${esc(item.vegan.note || "based on the description")}. This is not the restaurant's claim — check with staff.</p>` : ""}
          ${item.vegan?.status === "on_request" ? `<p class="banner good">The menu says: ${esc(item.vegan.note)}</p>` : ""}
          ${item.flags.length ? `<div class="banner warn">${item.flags.map((f) => esc(f.message)).join("<br>")}</div>` : ""}
          ${item.page_id ? `<button class="small" data-photo="${item.page_id}">View on the menu photo</button>` : ""}
          <div class="row" style="margin-top:.6rem">
            <button class="small" data-star="${item.id}" aria-pressed="${starred}">${starred ? "★ On shortlist" : "☆ Shortlist"}</button>
            <button class="primary grow" data-record="${item.id}" ${locked() ? "disabled" : ""}>Add to order</button>
          </div>
        </div>` : ""}
    </div>`;
}

function filterSheet() {
  const draw = () => `
    <div class="row between"><h2>Filter the menu</h2><button class="small" data-close>Done</button></div>
    <div class="filters wrapped">
      ${FILTERS.map(([k, label]) => `<button type="button" class="chip" data-f="${k}" aria-pressed="${ui.filters.has(k)}">${label}</button>`).join("")}
    </div>
    <label for="maxp">Price</label>
    <select id="maxp">
      <option value="">Any price</option>
      ${[10, 15, 20, 25, 30, 40].map((p) => `<option value="${p}" ${ui.maxPrice == p ? "selected" : ""}>Up to $${p}</option>`).join("")}
    </select>
    <p class="muted">“Vegan (menu)” is the restaurant's own label. “Possibly vegan” is our reading of the description — check with staff. None of this is allergy advice.</p>
    <div class="row" style="margin-top:1rem">
      <button type="button" class="grow" data-fclear>Clear</button>
      <button type="button" class="primary grow" data-close>Show ${S.menu.items.filter(passes).length} dishes</button>
    </div>`;
  const s = sheet(draw());
  const redraw = () => { s.el.innerHTML = draw(); renderMenu(); };
  s.el.addEventListener("click", (e) => {
    const f = e.target.closest("[data-f]");
    if (f) { ui.filters.has(f.dataset.f) ? ui.filters.delete(f.dataset.f) : ui.filters.add(f.dataset.f); redraw(); }
    if (e.target.closest("[data-fclear]")) { ui.filters.clear(); ui.maxPrice = ""; redraw(); }
  });
  s.el.addEventListener("change", (e) => { if (e.target.id === "maxp") { ui.maxPrice = e.target.value; redraw(); } });
}

// Underline the section you're reading in the "Jump to" bar.
let spy;
function watchSections() {
  spy?.disconnect();
  const links = $$(".catnav [data-jump]");
  if (!links.length || !("IntersectionObserver" in window)) return;
  spy = new IntersectionObserver((entries) => {
    const hit = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!hit) return;
    const id = hit.target.id.slice(4);
    links.forEach((a) => a.classList.toggle("on", a.dataset.jump === id));
    // Keep the highlighted name in view, but leave "Jump to" showing when it fits.
    const on = links.find((a) => a.dataset.jump === id);
    const nav = on?.parentElement;
    if (!nav) return;
    const left = on.offsetLeft - nav.offsetLeft;
    const label = nav.firstElementChild.offsetWidth + 16;
    if (left + on.offsetWidth > nav.scrollLeft + nav.clientWidth || left - label < nav.scrollLeft) {
      nav.scrollTo({ left: Math.max(0, left - label), behavior: "smooth" });
    }
  }, { rootMargin: "-30% 0px -60% 0px" });
  $$(".cat-head").forEach((h) => spy.observe(h));
}

function showPhoto(pageId) {
  const page = S.menu.pages.find((p) => p.id === pageId);
  if (!page?.image) return;
  sheet(`<div class="row between"><h2>Menu photo</h2><button class="small" data-close>Close</button></div>
    <img class="photo" src="${base}/image/${esc(page.image)}" alt="Photo of the menu page">`);
}

// ------------------------------------------------------------- recording
function recordSheet(item) {
  const extras = [...item.extras, ...(catById(item.category_id)?.extras || [])];
  const needsPrice = item.price_cents == null && !item.variants.length;
  const others = S.participants.filter((p) => p.id !== me());
  const forWho = S.organiser ? `
      <label for="for">Adding for</label>
      <select id="for">${S.participants.map((p) => `<option value="${p.id}" ${p.id === me() ? "selected" : ""}>${esc(p.name)}${p.id === me() ? " (you)" : ""}</option>`).join("")}</select>` : "";
  const s = sheet(`
    <div class="row between"><h2>Add to my order</h2><button class="small" data-close>Cancel</button></div>
    <p class="muted">Order with the staff as usual — this just keeps track so the bill can be split. Nothing goes to the kitchen.</p>
    <h3>${esc(item.name)}</h3>
    <form id="rec" class="stack">
      ${item.variants.length ? `<fieldset style="border:0;padding:0;margin:0"><legend class="muted">Choose one</legend>
        ${item.variants.map((v, i) => `<label class="check"><input type="radio" name="variant" value="${esc(v.label)}" ${i === 0 ? "checked" : ""}> ${esc(v.label)} <span class="grow"></span><span class="num">${v.price_cents != null ? money(v.price_cents) : "?"}</span></label>`).join("")}</fieldset>` : ""}
      ${extras.length ? `<fieldset style="border:0;padding:0;margin:0"><legend class="muted">Extras</legend>
        ${extras.map((e) => `<label class="check"><input type="checkbox" name="extra" value="${esc(e.label)}"> ${esc(e.label)} <span class="grow"></span><span class="num">${e.price_cents != null ? "+" + money(e.price_cents) : ""}</span></label>`).join("")}</fieldset>` : ""}
      ${needsPrice ? `<div><label for="mp">Price (the menu doesn't show one — ask staff)</label><input id="mp" inputmode="decimal" placeholder="e.g. 32.00"></div>` : ""}
      <div class="row between"><span>Quantity</span>
        <span class="stepper"><button type="button" data-q="-1" aria-label="Fewer">−</button><output id="qty">1</output><button type="button" data-q="1" aria-label="More">+</button></span></div>
      <div><label for="note">Note or change (optional)</label><input id="note" maxlength="300" placeholder="e.g. no chilli"></div>
      ${forWho}
      <label class="check"><input type="checkbox" id="shared"> Sharing this with others</label>
      <div id="sharebox" class="editor hidden">
        <p class="muted">Who's sharing? Everyone ticked pays an equal part.</p>
        <label class="check"><input type="checkbox" name="who" value="${me()}" checked> You</label>
        ${others.map((p) => `<label class="check"><input type="checkbox" name="who" value="${p.id}"> ${esc(p.name)}</label>`).join("")}
        <label class="check" id="unitsrow"><input type="checkbox" id="units"> Several of the same (e.g. drinks) — each person claims their own</label>
      </div>
      <div class="total-row big"><span>Total</span><span id="preview" class="num"></span></div>
      <button class="primary block">Add</button>
    </form>`);
  const el = s.el;
  let qty = 1;
  const read = () => {
    const variant = $("input[name=variant]:checked", el)?.value || "";
    const chosen = $$("input[name=extra]:checked", el).map((x) => x.value);
    let unit = item.variants.length ? item.variants.find((v) => v.label === variant)?.price_cents : item.price_cents;
    if (unit == null) unit = toCents($("#mp", el)?.value) ?? 0;
    const extraCents = chosen.reduce((a, l) => a + (extras.find((e) => e.label === l)?.price_cents || 0), 0);
    return { variant, chosen, total: (unit + extraCents) * qty };
  };
  const update = () => {
    $("#qty", el).textContent = qty;
    $("#preview", el).textContent = money(read().total);
    $("#unitsrow", el).classList.toggle("hidden", qty < 2);
  };
  el.addEventListener("click", (e) => {
    const q = e.target.closest("[data-q]");
    if (q) { qty = Math.max(1, Math.min(99, qty + parseInt(q.dataset.q, 10))); update(); }
  });
  el.addEventListener("input", update);
  $("#shared", el).addEventListener("change", (e) => $("#sharebox", el).classList.toggle("hidden", !e.target.checked));
  update();
  $("#rec", el).addEventListener("submit", (e) => {
    e.preventDefault();
    const r = read();
    const shared = $("#shared", el).checked;
    const forId = $("#for", el)?.value || me();
    const who = shared ? $$("input[name=who]:checked", el).map((x) => x.value) : [forId];
    if (!who.length) { toast("Tick at least one person.", true); return; }
    const units = shared && qty > 1 && $("#units", el).checked;
    const body = {
      menu_item_id: item.id, variant_label: r.variant, extras: r.chosen, quantity: qty,
      note: $("#note", el).value, shared, participants: units ? [forId] : who,
      split_mode: units ? "units" : "equal",
    };
    if (units) body.weights = { [forId]: 1 };
    if (item.price_cents == null && !item.variants.length) body.unit_price_cents = toCents($("#mp", el)?.value) ?? 0;
    outbox.push({ method: "POST", url: `${base}/lines`, body, label: item.name, done: `Added ${item.name}` });
    s.close();
  });
}

function manualSheet() {
  const s = sheet(`
    <div class="row between"><h2>Something not on the menu</h2><button class="small" data-close>Cancel</button></div>
    <p class="muted">Logs an item for the bill — it doesn't send anything to the kitchen.</p>
    <form id="man" class="stack">
      <div><label for="mn">What was it?</label><input id="mn" required maxlength="120" placeholder="e.g. Sparkling water"></div>
      <div class="field-row">
        <div><label for="mpr">Price each</label><input id="mpr" inputmode="decimal" required placeholder="0.00"></div>
        <div><label for="mq">Quantity</label><input id="mq" type="number" min="1" max="99" value="1"></div>
      </div>
      <label class="check"><input type="checkbox" id="msh"> Shared by everyone at the table</label>
      <button class="primary block">Add</button>
    </form>`);
  $("#man", s.el).addEventListener("submit", (e) => {
    e.preventDefault();
    const price = toCents($("#mpr", s.el).value);
    if (price == null) { toast("Enter the price, like 4.50", true); return; }
    const shared = $("#msh", s.el).checked;
    outbox.push({
      method: "POST", url: `${base}/lines`, label: $("#mn", s.el).value,
      body: { name: $("#mn", s.el).value, unit_price_cents: price, quantity: parseInt($("#mq", s.el).value, 10) || 1,
              shared, participants: shared ? S.participants.map((p) => p.id) : [me()] },
      done: "Added",
    });
    s.close();
  });
}

// The table is already sharing this dish: join theirs, or record your own?
function chooseJoinOrSeparate(op, existing) {
  const s = sheet(`
    <h2>Already on the table</h2>
    <p>${esc(op.label)} is already recorded as shared. Did you mean that one?</p>
    ${existing.map((l) => `<div class="card"><b>${esc(l.name)}${l.variant_label ? " · " + esc(l.variant_label) : ""}</b> ×${l.quantity}
      <div class="muted">Shared by ${l.allocations.map((a) => esc(nameOf(a.participant_id))).join(", ") || "nobody yet"}</div>
      <button class="primary block" data-joinline="${l.id}" style="margin-top:.5rem">Join this one</button></div>`).join("")}
    <button class="block" data-separate style="margin-top:.75rem">No — record a separate ${esc(op.label)}</button>
    <button class="block" data-close style="margin-top:.5rem">Cancel</button>`);
  s.el.addEventListener("click", (e) => {
    const j = e.target.closest("[data-joinline]");
    if (j) { s.close(); const line = S.lines.find((l) => l.id === j.dataset.joinline); if (line) joinSheet(line); }
    if (e.target.closest("[data-separate]")) {
      s.close();
      outbox.push({ method: "POST", url: op.url, body: { ...op.body, op_id: undefined, separate: true }, label: op.label, done: op.done });
    }
  });
}

// Joining always shows exactly how everyone's share changes before it happens.
function joinSheet(line) {
  const calc = lineCalc(line);
  if (line.split_mode === "units") return claimSheet(line);
  const weights = line.allocations.map((a) => [a.participant_id, a.weight]);
  const after = splitCents(calc.amount, [...weights, [me(), 1]]);
  const s = sheet(`
    <h2>Join ${esc(line.name)}?</h2>
    <p class="muted">Everyone's part changes like this:</p>
    <table class="plain"><tr><th>Person</th><th class="right">Now</th><th class="right">After</th></tr>
      ${weights.map(([pid]) => `<tr><td>${esc(nameOf(pid))}</td><td class="right num">${money(calc.shares[pid] || 0)}</td><td class="right num">${money(after[pid])}</td></tr>`).join("")}
      <tr><td><b>You</b></td><td class="right num">—</td><td class="right num"><b>${money(after[me()])}</b></td></tr></table>
    <div class="row" style="margin-top:1rem"><button class="grow" data-close>Cancel</button><button class="primary grow" data-go>Join</button></div>`);
  $("[data-go]", s.el).addEventListener("click", () => {
    outbox.push({ method: "POST", url: `${base}/lines/${line.id}/join`, body: { version: line.version }, label: line.name, done: `Joined ${line.name}` });
    s.close();
  });
}

function claimSheet(line) {
  const calc = lineCalc(line);
  const claimed = line.allocations.filter((a) => a.participant_id !== me()).reduce((a, x) => a + x.weight, 0);
  const mine = line.allocations.find((a) => a.participant_id === me())?.weight || 0;
  const free = line.quantity - claimed;
  const each = Math.round(calc.amount / line.quantity);
  const s = sheet(`
    <h2>${esc(line.name)} ×${line.quantity}</h2>
    <p class="muted">Each person claims how many were theirs (about ${money(each)} each).</p>
    <p>${line.allocations.filter((a) => a.participant_id !== me()).map((a) => `${esc(nameOf(a.participant_id))}: ${a.weight}`).join(" · ") || "Nobody else has claimed any."}</p>
    <div class="row between"><span>Mine</span><span class="stepper"><button type="button" data-q="-1">−</button><output id="u">${Math.max(mine, 1)}</output><button type="button" data-q="1">+</button></span></div>
    <p class="muted">${free} of ${line.quantity} available to you.</p>
    <div class="row" style="margin-top:1rem"><button class="grow" data-close>Cancel</button><button class="primary grow" data-go>Save</button></div>`);
  let u = Math.max(Math.min(mine || 1, free), 1);
  $("#u", s.el).textContent = u;
  s.el.addEventListener("click", (e) => {
    const q = e.target.closest("[data-q]");
    if (q) { u = Math.max(1, Math.min(free, u + parseInt(q.dataset.q, 10))); $("#u", s.el).textContent = u; }
  });
  $("[data-go]", s.el).addEventListener("click", () => {
    outbox.push({ method: "POST", url: `${base}/lines/${line.id}/join`, body: { version: line.version, units: u }, label: line.name, done: "Saved" });
    s.close();
  });
}

function leave(line) {
  const others = line.allocations.filter((a) => a.participant_id !== me());
  const s = sheet(`
    <h2>Leave ${esc(line.name)}?</h2>
    <p>${others.length ? `${others.map((a) => esc(nameOf(a.participant_id))).join(", ")} will cover your part.` : "Nobody else is on it — it will be left for someone to claim."}</p>
    <div class="row"><button class="grow" data-close>Cancel</button><button class="primary grow" data-go>Leave</button></div>`);
  $("[data-go]", s.el).addEventListener("click", () => {
    outbox.push({ method: "POST", url: `${base}/lines/${line.id}/leave`, body: { version: line.version }, label: line.name, done: "Done" });
    s.close();
  });
}

// Straight from My order: one tap, then a confirm so a stray tap can't lose it.
function removeSheet(line) {
  const s = sheet(`
    <h2>Remove ${esc(line.name)}?</h2>
    <p class="muted">It comes off the bill. If you did order it, someone will need to add it back.</p>
    <div class="row"><button class="grow" data-close>Keep it</button><button class="primary grow" data-go>Remove</button></div>`);
  $("[data-go]", s.el).addEventListener("click", () => {
    outbox.push({ method: "DELETE", url: `${base}/lines/${line.id}?version=${line.version}`, label: line.name, done: "Removed" });
    s.close();
  });
}

function editSheet(line) {
  const item = line.menu_item_id ? itemById(line.menu_item_id) : null;
  const s = sheet(`
    <div class="row between"><h2>${esc(line.name)}</h2><button class="small" data-close>Cancel</button></div>
    <form id="ed" class="stack">
      <div class="field-row">
        <div><label for="eq">Quantity</label><input id="eq" type="number" min="1" max="99" value="${line.quantity}"></div>
        ${!item || (item.price_cents == null && !item.variants.length) ? `<div><label for="ep">Price each</label><input id="ep" inputmode="decimal" value="${(line.unit_price_cents / 100).toFixed(2)}"></div>` : ""}
      </div>
      <div><label for="en">Note</label><input id="en" maxlength="300" value="${esc(line.note)}"></div>
      <button class="primary block">Save</button>
      <button type="button" class="danger block" data-del>Remove from the bill</button>
    </form>`);
  $("#ed", s.el).addEventListener("submit", (e) => {
    e.preventDefault();
    const body = { version: line.version, quantity: parseInt($("#eq", s.el).value, 10), note: $("#en", s.el).value };
    const p = $("#ep", s.el);
    if (p) { const c = toCents(p.value); if (c == null) { toast("Enter a price like 4.50", true); return; } body.unit_price_cents = c; }
    outbox.push({ method: "PATCH", url: `${base}/lines/${line.id}`, body, label: line.name, done: "Saved" });
    s.close();
  });
  $("[data-del]", s.el).addEventListener("click", () => {
    outbox.push({ method: "DELETE", url: `${base}/lines/${line.id}?version=${line.version}`, label: line.name, done: "Removed" });
    s.close();
  });
}

// ------------------------------------------------------------------ mine
function lockedBanner() {
  if (S.dinner.status === "finalised") return `<div class="banner info">The bill is final. <a href="#" data-gotab="pay">See what to pay</a>.</div>`;
  if (S.dinner.reopened) return `<div class="banner warn">The organiser reopened the bill to fix something. Amounts may change — hold off paying until it's final again.</div>`;
  return "";
}

function lineRow(l, { forMe }) {
  const c = lineCalc(l);
  const allocs = l.allocations;
  const mine = allocs.find((a) => a.participant_id === me());
  const who = l.split_mode === "units"
    ? `${allocs.map((a) => `${esc(nameOf(a.participant_id))} ${a.weight}`).join(" · ")}${c.unallocated ? ` · <b>${l.quantity - allocs.reduce((a, x) => a + x.weight, 0)} unclaimed</b>` : ""}`
    : l.split_mode === "proportional" ? "Split in proportion to everyone's items"
    : allocs.length ? allocs.map((a) => esc(nameOf(a.participant_id))).join(", ") : "<b>Nobody yet</b>";
  const canEdit = S.organiser || l.created_by === me() || (allocs.length === 1 && mine);
  const solo = (allocs.length === 1 && mine) || (!allocs.length && l.created_by === me());
  const actions = locked() ? "" : [
    !mine && l.split_mode !== "proportional" && (l.split_mode !== "units" || c.unallocated) ? `<button class="small" data-join="${l.id}">${l.split_mode === "units" ? "Claim" : allocs.length ? "Join" : "It's mine"}</button>` : "",
    mine && l.split_mode === "units" ? `<button class="small" data-join="${l.id}">Change</button>` : "",
    mine && allocs.length > 1 ? `<button class="small" data-leave="${l.id}">${forMe ? "Remove me" : "Leave"}</button>` : "",
    canEdit && l.kind === "item" ? `<button class="small" data-edit="${l.id}">Edit</button>` : "",
    forMe && solo && canEdit && l.kind === "item" ? `<button class="small danger" data-remove="${l.id}">Remove</button>` : "",
  ].join("");
  const amountMine = forMe ? money(c.shares[me()] || 0) : money(c.amount);
  return `
    <div class="line">
      <div class="grow">
        <div><b>${esc(l.name)}</b>${l.variant_label ? ` · ${esc(l.variant_label)}` : ""}${l.quantity > 1 ? ` ×${l.quantity}` : ""}</div>
        ${l.extras.length ? `<div class="who">+ ${l.extras.map((e) => esc(e.label)).join(", ")}</div>` : ""}
        ${l.note ? `<div class="who">“${esc(l.note)}”</div>` : ""}
        <div class="who">${forMe && allocs.length > 1 ? `Your part of ${money(c.amount)} · ` : ""}${who}</div>
        ${actions ? `<div class="row" style="margin-top:.35rem">${actions}</div>` : ""}
      </div>
      <div class="num"><b>${amountMine}</b></div>
    </div>`;
}

function renderMine() {
  const mineLines = S.lines.filter((l) => l.allocations.some((a) => a.participant_id === me()) || (l.created_by === me() && !l.allocations.length));
  const totals = S.bill.people[me()] || { items: 0, adjustments: 0, total: 0 };
  app.innerHTML = `
    <div class="wrap">
      ${lockedBanner()}
      <div class="row between" style="margin-top:1rem"><h1 style="margin:0">My order</h1>
        ${mineLines.length ? `<button class="primary" data-staff="mine">Show to staff</button>` : ""}</div>
      ${outbox.size ? `<div class="banner warn">${outbox.size} change${outbox.size > 1 ? "s" : ""} waiting to send${outbox.failing ? " — no connection yet, will retry" : ""}.</div>` : ""}
      <div class="card">
        ${mineLines.length ? mineLines.map((l) => lineRow(l, { forMe: true })).join("") : `<p class="muted">Nothing here yet. Find a dish on the menu and tap <b>Add to order</b>. Shortlisting doesn't add anything to the bill.</p>`}
      </div>
      <div class="card">
        <div class="total-row"><span>My items</span><span class="num">${money(totals.items)}</span></div>
        ${totals.adjustments ? `<div class="total-row"><span>My part of surcharges/discounts</span><span class="num">${money(totals.adjustments)}</span></div>` : ""}
        <div class="total-row big"><span>Running total</span><span class="num">${money(totals.total)}</span></div>
        <p class="muted">Final amounts are confirmed when the organiser checks the restaurant's bill.</p>
      </div>
      <section class="card">
        <div class="row between"><span>Your name</span><b>${esc(S.me.name)}</b></div>
        <button class="small" data-rename style="margin-top:.5rem">Change name</button>
      </section>
    </div>`;
}

// ------------------------------------------------------------ staff view
// Big, plain and price-free: hold the phone up while ordering. Shared dishes
// say who they're with, so the next person doesn't order them again.
let wake = null;
function staffView(which) {
  const mine = which === "mine";
  const lines = S.lines.filter((l) => l.kind === "item" && (!mine || l.allocations.some((a) => a.participant_id === me()) || (l.created_by === me() && !l.allocations.length)));
  const row = (l) => {
    const units = l.split_mode === "units";
    const qty = mine && units ? l.allocations.find((a) => a.participant_id === me())?.weight || l.quantity : l.quantity;
    const others = l.allocations.filter((a) => a.participant_id !== me()).map((a) => nameOf(a.participant_id));
    const shared = mine && !units && others.length ? `Shared with ${others.map(esc).join(", ")}` : "";
    return `<div class="staff-line"><span class="staff-qty">${qty}×</span><div class="grow">
      <div class="staff-name">${esc(l.name)}${l.variant_label ? ` <span>· ${esc(l.variant_label)}</span>` : ""}</div>
      ${l.extras.length ? `<div class="staff-sub">+ ${l.extras.map((e) => esc(e.label)).join(", ")}</div>` : ""}
      ${l.note ? `<div class="staff-sub">“${esc(l.note)}”</div>` : ""}
      ${shared ? `<div class="staff-who">${shared}</div>` : ""}</div></div>`;
  };
  const el = document.createElement("div");
  el.className = "staff-view";
  el.setAttribute("role", "dialog");
  el.innerHTML = `
    <div class="row between"><div class="muted">${mine ? esc(S.me.name) : "Whole table"}</div><button data-close>Done</button></div>
    ${lines.map(row).join("") || '<p class="muted">Nothing added yet.</p>'}`;
  const close = () => { el.remove(); wake?.release?.().catch(() => {}); wake = null; };
  el.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) close(); });
  document.body.append(el);
  // Keep the screen on while it's being read, where the phone allows it.
  navigator.wakeLock?.request("screen").then((w) => { wake = w; }).catch(() => {});
}

// ----------------------------------------------------------------- table
function renderTable() {
  const b = S.bill;
  const unclaimed = S.lines.filter((l) => lineCalc(l).unallocated);
  app.innerHTML = `
    <div class="wrap">
      ${lockedBanner()}
      <div class="row between" style="margin-top:1rem"><h1 style="margin:0">The table</h1>
        ${S.lines.some((l) => l.kind === "item") ? `<button data-staff="table">Show to staff</button>` : ""}</div>
      <p class="muted">Everything recorded so far. Join a shared dish here rather than recording it twice.</p>
      ${unclaimed.length ? `<div class="banner warn">${unclaimed.length} item${unclaimed.length > 1 ? "s haven't" : " hasn't"} been claimed yet.</div>` : ""}
      <div class="card">${S.lines.length ? S.lines.map((l) => lineRow(l, { forMe: false })).join("") : '<p class="muted">Nothing recorded yet.</p>'}</div>
      <h2 style="margin-top:1.25rem">Running totals</h2>
      <div class="card">
        ${S.participants.map((p) => `<div class="total-row"><span>${esc(p.name)}${p.id === me() ? " (you)" : ""}</span><span class="num">${money(b.people[p.id]?.total || 0)}</span></div>`).join("")}
        <div class="total-row big"><span>Ordered so far</span><span class="num">${money(b.recorded_total)}</span></div>
        ${b.bill_total != null ? `<div class="total-row"><span>Restaurant's total</span><span class="num">${money(b.bill_total)}</span></div>` : ""}
      </div>
      ${S.receipt_image ? `<p><button class="small" data-receipt>View the receipt</button></p>` : ""}
    </div>`;
}

// ------------------------------------------------------------------- pay
const STATE_TEXT = {
  awaiting: ["Awaiting payment", "warn"],
  marked_sent: ["You said you've sent it — waiting for it to arrive", "info"],
  part_paid: ["Part received", "warn"],
  confirmed: ["Received — thank you!", "good"],
  overpaid: ["Received more than your share", "info"],
  nothing_owed: ["Nothing to pay", "good"],
  self: ["You paid the restaurant", "good"],
};

function renderPay() {
  const p = S.payment;
  if (!p) {
    const mine = S.bill.people[me()]?.total || 0;
    app.innerHTML = `<div class="wrap">${lockedBanner()}<h1 style="margin-top:1rem">Paying back</h1>
      <div class="card"><p>The bill isn't final yet. When the organiser has checked it against the restaurant's total, your amount and payment details will appear here.</p>
      <div class="total-row big"><span>Your running total</span><span class="num">${money(mine)}</span></div></div></div>`;
    return;
  }
  const [label, tone] = STATE_TEXT[p.state] || ["", ""];
  const ins = p.instructions;
  const due = Math.max(p.balance, 0);
  let body;
  if (p.state === "self") {
    body = `<div class="card"><p>You paid the restaurant. Your share of <b>${money(p.share)}</b> counts toward the total — there's nothing to transfer to yourself.</p></div>`;
  } else if (!ins) {
    body = `<div class="card"><div class="banner warn">The organiser reopened the bill. Payment details will come back once it's final again.</div>
      <div class="stat"><dt>Received so far</dt><dd>${money(p.received)}</dd></div></div>`;
  } else {
    body = `
      <div class="card center">
        <div class="muted">${p.received ? "Still to pay" : "Your share"}</div>
        <div class="amount-hero">${money(due)}</div>
        ${p.received ? `<div class="muted">Share ${money(p.share)} · received ${money(p.received)}</div>` : ""}
        ${p.state === "overpaid" ? `<div class="banner info">You've paid ${money(-p.balance)} more than your share — the organiser will sort it out with you.</div>` : ""}
      </div>
      ${due > 0 ? `
      <div class="card">
        <p class="muted">Pay by PayID in your banking app, then come back and tap “I've sent it”.</p>
        ${ins.payid ? `<div class="copyrow"><div class="grow"><div class="k">PayID${PAYID_LABEL[ins.payid_type] ? ` (${PAYID_LABEL[ins.payid_type]})` : ""}</div><div class="v">${esc(ins.payid)}</div></div><button class="small" data-copy="${esc(ins.payid)}" data-what="PayID">Copy PayID</button></div>`
          : `<div class="banner warn">The organiser hasn't added their PayID yet — ask them where to pay.</div>`}
        <div class="copyrow"><div class="grow"><div class="k">Amount</div><div class="v num">${money(due)}</div></div><button class="small" data-copy="${(due / 100).toFixed(2)}" data-what="Amount">Copy amount</button></div>
        <div class="copyrow"><div class="grow"><div class="k">Reference (so it's matched to you)</div><div class="v">${esc(ins.reference)}</div></div><button class="small" data-copy="${esc(ins.reference)}" data-what="Reference">Copy reference</button></div>
        <p class="muted">Your bank should show the name <b>${esc(ins.recipient_name || "—")}</b> before you confirm. If it shows a different name, don't send — check with the organiser.</p>
        ${ins.bsb && ins.account_number ? `<details><summary>No PayID in your bank? Use BSB and account</summary>
          <div class="copyrow"><div class="grow"><div class="k">BSB</div><div class="v">${esc(ins.bsb)}</div></div><button class="small" data-copy="${esc(ins.bsb)}" data-what="BSB">Copy</button></div>
          <div class="copyrow"><div class="grow"><div class="k">Account</div><div class="v">${esc(ins.account_number)}</div></div><button class="small" data-copy="${esc(ins.account_number)}" data-what="Account number">Copy</button></div>
          <div class="copyrow"><div class="grow"><div class="k">Account name</div><div class="v">${esc(ins.recipient_name)}</div></div></div></details>` : ""}
      </div>
      <button class="${p.state === "marked_sent" ? "" : "primary"} block" data-sent="${p.state === "marked_sent" ? "0" : "1"}">${p.state === "marked_sent" ? "Actually, I haven't sent it yet" : "I've sent it"}</button>` : ""}`;
  }
  app.innerHTML = `
    <div class="wrap">
      <h1 style="margin-top:1rem">Paying back</h1>
      <p><span class="state ${tone}">${esc(label)}</span></p>
      ${body}
      <p class="muted" style="margin-top:1rem">“I've sent it” just tells the organiser to look out for it. Your status changes to received once the money actually arrives.</p>
    </div>`;
}
const PAYID_LABEL = { phone: "mobile number", email: "email", abn: "ABN", org_id: "organisation ID" };

// ---------------------------------------------------------------- events
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-tab],[data-gotab],[data-filters],[data-clearprice],[data-filter],[data-jump],[data-toggle],[data-star],[data-record],[data-photo],[data-manual],[data-join],[data-leave],[data-remove],[data-staff],[data-edit],[data-copy],[data-sent],[data-rename],[data-receipt]");
  if (!t || !S) return;
  const d = t.dataset;
  if (d.tab) setTab(d.tab);
  else if (d.gotab) { e.preventDefault(); setTab(d.gotab); }
  else if (d.filter) { ui.filters.has(d.filter) ? ui.filters.delete(d.filter) : ui.filters.add(d.filter); renderMenu(); }
  else if (d.filters !== undefined) filterSheet();
  else if (d.clearprice !== undefined) { ui.maxPrice = ""; renderMenu(); }
  else if (d.jump) { e.preventDefault(); $(`#cat-${CSS.escape(d.jump)}`)?.scrollIntoView({ behavior: "smooth" }); }
  else if (d.toggle) { ui.open.has(d.toggle) ? ui.open.delete(d.toggle) : ui.open.add(d.toggle); renderMenu(); }
  else if (d.star) {
    const on = !S.shortlist.includes(d.star);
    S.shortlist = on ? [...S.shortlist, d.star] : S.shortlist.filter((x) => x !== d.star);
    renderMenu();
    outbox.push({ method: "POST", url: `${base}/shortlist`, body: { menu_item_id: d.star, on }, label: "Shortlist" });
  } else if (d.record) {
    const item = itemById(d.record);
    if (item?.unavailable && !S.organiser) toast(`${item.name} is marked unavailable tonight.`, true);
    else if (item) recordSheet(item);
  } else if (d.photo) showPhoto(d.photo);
  else if (d.manual) manualSheet();
  else if (d.join) { const l = S.lines.find((x) => x.id === d.join); if (l) joinSheet(l); }
  else if (d.staff) staffView(d.staff);
  else if (d.remove) { const l = S.lines.find((x) => x.id === d.remove); if (l) removeSheet(l); }
  else if (d.leave) { const l = S.lines.find((x) => x.id === d.leave); if (l) leave(l); }
  else if (d.edit) { const l = S.lines.find((x) => x.id === d.edit); if (l) editSheet(l); }
  else if (d.copy !== undefined) copy(d.copy, d.what);
  else if (d.sent) {
    t.disabled = true;
    try { await api("POST", `${base}/sent`, { sent: d.sent === "1" }); await refresh(); }
    catch (err) { toast(explainError(err), true); t.disabled = false; }
  } else if (d.rename) {
    const s = sheet(`<h2>Your name</h2><form id="rn" class="stack">
      <input id="rnv" maxlength="40" required value="${esc(S.me.name)}">
      <p class="muted">Your payment reference stays ${esc(S.me.reference)} either way.</p>
      <div class="row"><button type="button" class="grow" data-close>Cancel</button><button class="primary grow">Save</button></div></form>`);
    $("#rn", s.el).addEventListener("submit", async (ev) => {
      ev.preventDefault();
      try { await api("POST", `${base}/me`, { name: $("#rnv", s.el).value }); s.close(); refresh(); }
      catch (err) { toast(explainError(err), true); }
    });
  } else if (d.receipt) {
    sheet(`<div class="row between"><h2>Receipt</h2><button class="small" data-close>Close</button></div><img class="photo" src="${base}/image/${esc(S.receipt_image)}" alt="Receipt photo">`);
  }
});

document.addEventListener("input", (e) => {
  if (e.target.id === "q") {
    ui.q = e.target.value;
    const pos = e.target.selectionStart;
    renderMenu();
    const q = $("#q");
    q.focus();
    q.setSelectionRange(pos, pos);
  }
});

prefsControls($("#prefs"));
refresh().then(() => {
  live(`${base}/events`, {
    onRevision: () => refreshSoon(),
    onStatus: (ok) => { online = ok; renderConn(); if (ok) outbox.flush(); },
  });
  outbox.flush();
});
window.addEventListener("online", () => outbox.flush());
