# Task 3 Part C — Front-End Dashboard

> Deliverable for: *"Create a simple HTML/JavaScript dashboard with KPI cards, two charts and a table. The front-end must consume the API rather than directly reading CSV files."*

Implementation: `frontend/index.html` — one file, no build step, no external dependency.

---

## 1. What it shows

| Region | Content | API call |
|---|---|---|
| Header | date range, merchant filter, API key, Load | — |
| Gap hero | the unsettled amount as the headline number, with a proportional settled-vs-missing bar | `/api/v1/settlement-summary` |
| KPI strip | Transaction volume, Settlement rate, Settlement gap, SLA rate, Merchants at risk | `/api/v1/settlement-summary` + `/api/v1/merchant-exceptions` |
| Chart 1 | daily transaction amount vs daily settled amount, grouped bars | `/api/v1/daily-trend` |
| Chart 2 | top 10 merchants by settlement gap, horizontal bars | `/api/v1/top-merchant-gaps` |
| Table | merchant, risk at period end, settlement rate, SLA rate, unsettled count, gap | `/api/v1/merchant-exceptions` |
| Data quality panel | rule, source, handling, records, plain-English meaning | `/api/v1/data-quality` |

All five requests fire in parallel through `Promise.all`, so the page renders in one round trip's time rather than five.

## 2. Design decisions worth explaining

**The hero is the gap, not the volume.** The Head of Payments already knows the volume. The unanswered question is the difference, so the largest element on the page is the unsettled rupee amount with a sentence that decomposes it: how much settled, across how many transactions, and how many have no settled record at all. The bar underneath is a single proportional strip — solid green for money that arrived, hatched red for money that did not — so the ratio is readable without reading a number.

**Indian money formatting.** Values render as ₹ Cr / ₹ L, because a payments operations team in India reads ₹4.32 Cr instantly and `432558048.71` not at all.

**Breach colouring is rule-driven, not decorative.** A KPI card turns red only when it crosses the documented threshold (95% settlement, 90% SLA), and the same thresholds colour the table cells. Colour is never the only signal — the breach reason is also written in text, and the exception table states the rule under it.

**Zero external dependencies.** No CDN, no chart library, no fonts fetched at runtime. Charts are hand-built inline SVG with `<title>` elements for native tooltips. This matters for three reasons in a bank: the page works on an air-gapped desktop, it needs no CSP exceptions, and there is no third-party JavaScript executing next to settlement data.

**Tabular numerals and right-aligned figures** so columns of money compare vertically. System font stack, no webfont request.

**Failure and empty states give direction.** An API error names the endpoint state and tells the user what to check; an empty result says "no transactions in this window — widen the dates or clear the merchant filter" rather than showing a blank panel.

**Accessibility floor.** Visible keyboard focus, labelled inputs, `role="img"` with `aria-label` on both charts, `role="alert"` on the error banner, responsive down to a phone, and motion respected via `prefers-reduced-motion`.

## 3. It consumes the API, provably

The file contains no file input, no CSV parsing and no data literals. Every number on screen arrives from `fetch()` against `API_BASE`. Pointing it at a deployed API is one line:

```js
const API_BASE = window.API_BASE || "http://127.0.0.1:8000";
```

In production the dashboard is served as a static asset (S3 + CloudFront, or the same container) and `window.API_BASE` is injected per environment.

## 4. Security handling in the browser

- The API key is held in a password input and kept in memory only — never written to `localStorage`, never in the URL. In production the browser holds an OIDC session cookie and this field disappears entirely.
- Every value interpolated into the DOM goes through an `esc()` HTML-escaper, so a merchant name containing markup cannot execute.
- The dashboard is served from an origin on the API's CORS allow-list; the API permits GET only and no credentials.
- No customer data is available to display, because no endpoint returns any.

## 5. Running it

```bash
make api      # terminal 1: http://127.0.0.1:8000
make ui       # terminal 2: http://127.0.0.1:8080
```
Open `http://127.0.0.1:8080`, keep the default window of 2026-09-01 to 2026-09-07, and press Load.

## 6. What a production version would add

Server-side session auth instead of an API key field, a merchant type-ahead backed by a lookup endpoint, drill-through from a table row to that merchant's transaction-level exceptions, CSV export of the exception queue, a "data as of" badge driven by `/ready`, and auto-refresh on a 15 minute timer aligned to the pipeline schedule.
