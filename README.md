# Dinner Tab

Photograph the menu, show guests a QR code, let everyone record what they
order and split shared dishes, reconcile with the restaurant's bill, and get
paid back by PayID — with Up Bank spotting the transfers for you.

- **You (organiser):** an installable web app at `/o`, password protected.
- **Guests:** scan a QR code and use an ordinary webpage. No app, no account.

---

## How a dinner goes

1. **Arrive early → New dinner.** Photograph the menu pages (Menu tab), or choose
   the restaurant's PDF menu if it's online — each PDF page is read like a photo.
   The QR code is ready straight away — share it while the pages are still being read.
   **Been there before?** Type the restaurant's name and the app offers the menu
   saved last time — guests see it immediately. If the menu has changed, scan it
   again: matching dishes are updated in place with a note of what changed
   ("Price was $24.00", "New since last visit"), and saved dishes the new scan
   didn't find are listed so you can remove them. Each dinner keeps its own copy,
   so an old bill never changes. Tonight-only specials aren't saved.
2. **Review each page** as it's read: fix anything flagged (unclear text, a price
   that may belong to another line, a missing price), then **Publish to guests**.
   Guests see published pages immediately; unpublished pages show as "being read".
   Add specials or corrections by hand at any time.
3. **Guests join** with a display name and browse a plain list of dishes and
   prices — tap one for its description, options and dietary notes. Search, a
   Filter button, a "Jump to" section bar and an "Aa" display button (text size,
   light/dark) sit above it. Shortlist, and tap **Add to order**. Adding only keeps track of what
   they ordered with staff; it doesn't send anything to the kitchen.
4. **Shared dishes** are recorded once. Anyone else tapping the same dish is asked
   to *join* it, with a preview of how everyone's part changes. Several of the
   same thing (e.g. three pints) can be claimed per unit.
5. **At the end**, either:
   - **scan the itemised receipt** (Receipt tab), confirm the suggested matches,
     add anything nobody recorded, and deal with recorded items the receipt
     doesn't show; or
   - **type the total the restaurant quotes** (Bill tab) and resolve any
     difference by correcting items or adding a *labelled* adjustment.
6. **Finalise** is enabled only when the Bill tab says **Everything accounted
   for** (total matches, every cent allocated). Guests then see their share,
   your PayID, and their own reference, each with a Copy button.
7. **Payments** turn green automatically when Up sees them (or when you mark them
   received). Ambiguous transfers wait in your review list.

Receipt-first dinners skip step 1–4: scan the receipt, **Add all unresolved
lines as items**, and guests claim them on the Table tab.

## Try it without spending anything

Sign in and press **Create demo dinner**. You get a two-page menu (with the
awkward cases: variants, extras, vegan/vegetarian distinctions, an unclear price,
market price, glare), five guests including two Helens and a Helen G, recorded
orders with a shared dish and per-unit pints, and a **sample receipt** button
that exercises abbreviations, a changed price, an unrecorded item, a missing
item, a surcharge and GST-included. After finalising, **Simulate a transfer**
runs the real matching rules on pretend payments — demo dinners can never match
real Up transfers, and real dinners never match simulated ones.

No Anthropic key and no Up token are needed for the demo.

---

## Setup

### Local

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env            # then set SECRET_KEY and ADMIN_PASSWORD
uvicorn app.main:app --port 8080
```

Open http://127.0.0.1:8080/o. Guests on the same Wi-Fi can use your LAN address
if you set `PUBLIC_BASE_URL=http://<your-ip>:8080`.

Tests: `pytest` (106 tests, no network). Lint: `ruff check . && ruff format --check .`

### Server (do-personal)

https://dinner.appfoundry.cc — a systemd service behind nginx on do-personal,
like menuvi and mealops. Push to `main` deploys via GitHub Actions (lint → tests
→ SSH deploy → wait for healthy). Details: [`deploy/SERVER-SETUP.md`](deploy/SERVER-SETUP.md).

**Run exactly one app process.** Live updates and the lock that serialises
simultaneous edits live in memory, so the service runs a single uvicorn worker.
That comfortably serves a table of friends.

---

## Credentials

| What | Where it goes | Needed for | Notes |
|---|---|---|---|
| `SECRET_KEY` | server `.env` | always | Long random string. Signs cookies, encrypts the Up webhook secret. Don't change it casually. |
| `ADMIN_PASSWORD` | server `.env` | organiser sign-in | 12+ chars in production. 5 attempts / 5 min. |
| `ANTHROPIC_API_KEY` | server `.env` | reading menu & receipt photos | Optional. Without it you type items in. |
| `UP_API_TOKEN` | server `.env` | automatic payment detection | Optional. Up app → *Data sharing* → *Personal Access Token*. |
| PayID, name, BSB/account | Settings page | guest payment screen | Stored in the app database; shown only after finalising. |

The Up token and Anthropic key exist only in the server environment: never in a
page, the browser, the service worker cache, or git (`.env` is ignored).

### Connecting Up

1. Put `UP_API_TOKEN` in `.env`, restart.
2. Settings → **Choose receiving account** (the account your PayID pays into).
3. Settings → **Register webhook** (needs the public HTTPS address in
   `PUBLIC_BASE_URL`). The app creates the webhook, stores Up's one-time secret
   encrypted, and sends a test ping.
4. Even without the webhook, the app re-reads recent transactions every
   `UP_POLL_MINUTES` while any bill is awaiting payment, and **Check now** does it
   on demand.

**Do a real $1 test transfer first** (from another bank, with a reference) and
look at how it appears in the Payments review list. See the limitations below
for why.

---

## Costs

| Item | Cost |
|---|---|
| Hosting | $0 extra — runs on the existing do-personal droplet. |
| Domain / TLS | $0 — a subdomain of appfoundry.cc; certbot (Let's Encrypt) certificate. |
| Up API | Free. |
| Photo reading (Claude Opus 5.5, $4 / $20 per million tokens in/out) | Roughly **10–25¢ per menu page** and similar per receipt, depending on how dense the page is. A typical dinner (3 pages + receipt) is well under **$1**. Set `EXTRACTION_EFFORT=medium` to trim it. |

---

## How the important rules are enforced

- **Money is integer cents.** Every split uses largest remainder with a fixed
  order (join order), so shares always add up exactly to the bill.
- **Prices come from the menu on the server**, never from a phone. A guest can
  only enter a price for something the menu doesn't price.
- **No silent overwrites.** Every edit carries the version the person was looking
  at; if someone else changed it first, the edit is refused and the screen
  refreshes. Finalising requires the exact bill the organiser reviewed.
- **No duplicates on bad signal.** Each change has an id; a retry after a dropped
  reply returns the first result. Changes made offline wait in a visible
  "N waiting" queue and send in order when the connection returns.
- **No double counting.** A confirmed receipt match keeps the recorded lines and
  everyone's shares; only unmatched receipt lines can be added. A recorded line
  can be matched to one receipt line at most.
- **No silent spreading.** A difference between the restaurant's total and the
  recorded items blocks finalisation until items are corrected or a visible,
  named adjustment line is added. GST-included lines are informational.
- **Surcharges and discounts** are split in proportion to each person's items by
  default; the organiser can change that per line.
- **Reopening** after payment details went out needs explicit confirmation;
  payments already received are kept and new balances or overpayments shown.
- **Payment matching** is automatic only when (a) exactly one outstanding person's
  reference is in the transfer, standing on its own, with the exact amount owed;
  or (b) there's no reference, exactly one person owes exactly that amount, only
  one dinner is collecting, and nothing in the text points elsewhere. Everything
  else goes to review with the reasons shown. A transfer can back one payment
  only (also enforced in the database). Every payment records its reason and
  transaction id; undo is kept in the history.
- **Privacy.** Guests see the shared orders and totals, plus only *their own*
  reference and payment status. Only credits into the chosen account, during an
  open payment window, are stored — never debits, balances or other accounts.
  Menu and receipt photos are served only to people at that dinner.
- **Sessions.** Guests get a random session cookie; knowing a name gets you
  nothing (a second "Helen" is just another Helen). A guest on a new phone gets a
  single-use personal link from the organiser (People on the Share tab).

---

## Limitations (verified against official documentation)

**Up Bank** (developer.up.com.au and Up's published OpenAPI spec, v1):

- Personal access tokens only: the API reads *your own* accounts. One token is
  active at a time, and you choose its lifetime when creating it — when it
  expires, detection stops (Settings shows the error) until you paste a new one.
- Webhook events contain only the event type and a transaction id/link; the app
  fetches each transaction. Up waits 30 s and retries non-200 responses. Up notes
  that "settled" may occasionally arrive as delete + create instead.
- **Up does not document where an incoming PayID/Osko transfer's reference or
  the payer's name appears.** The relevant fields are described generically:
  `message` ("payment message, or a transfer note"), `description` ("usually
  the merchant name for purchases") and `rawText`. The app searches all three for
  the reference and treats a name only as supporting evidence. Confirm with a
  real test transfer; if references turn out not to come through, matching falls
  back to the unique-exact-amount rule and your review list.
- Webhooks need a public HTTPS URL (≤ 300 characters); max 10 webhooks per token.

**PayID:** there is no universal PayID payment link or QR format that all
Australian banking apps open, so guests copy the PayID, amount and reference
into their own app. The screen tells them which recipient name to expect.

**Other:**
- One app process only (see above).
- iPhone HEIC photos: Safari normally converts to JPEG on upload; if a file is
  rejected, set Camera → Formats → *Most Compatible*.
- Photo reading can be wrong. Everything it's unsure of is flagged, "possibly
  vegan" is always labelled as the app's reading (not the restaurant's claim),
  and nothing here is allergy advice.

## Layout

```
app/
  main.py           app factory, security headers, cross-site guard, Up sync loop
  config.py         environment settings (secrets refused if weak in production)
  models.py         tables — money in cents, versions on everything editable
  security.py       organiser sign-in, guest sessions, rate limits, encryption
  routes/           guest.py (/d, /api/d), organiser.py (/o, /api/o), webhooks.py
  services/
    billing.py      who owes what (one function drives every screen)
    money.py        largest-remainder splitting
    references.py   dinner codes and payment references
    orders.py       recording, sharing, joining, per-unit claims
    menu.py         pages, extraction results, corrections
    restaurants.py  each restaurant's saved menu, loading it, rescans
    extraction.py   Claude photo reading + verification of its output
    reconcile.py    receipt matching
    finalise.py     locking / reopening
    payments.py     payment status and transfer matching
    up.py           Up API client, webhooks, catch-up sync
    demo.py         the demo dinner and sample photos
  static/, templates/   plain JS modules, no build step
tests/              106 tests
deploy/             systemd unit, nginx site, deploy script, server notes
```
