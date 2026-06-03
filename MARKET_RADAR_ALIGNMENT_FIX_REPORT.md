# Market Radar — "Strongest Setups Today" Table Alignment Fix

**Page:** `/market-radar`
**Scope:** frontend only (HTML/CSS/JS embedded in `_market_radar_page_html()`).
**No trading logic, no backend calculations, no API changes.**

---

## Problem

The "Strongest Setups Today" rows were not evenly aligned — the LONG/SHORT badge,
confidence, RR, and status columns floated and drifted from row to row.

### Root cause
The list rendered each row as a **flexbox** with `justify-content:space-between`:

```css
.mr-setup-row{display:flex;justify-content:space-between;align-items:center;...}
```

```js
'<div class="mr-setup-row">' +
'<span>…symbol…</span><span>…side…</span><span>…conf…</span><span>…rr…</span><span>…status…</span>'
```

With `space-between`, each `<span>` sizes to its own content and the gaps are distributed
by the remaining width. Because every row has different text lengths (e.g. `89%` vs `100%`,
`1:2.29` vs `1:3.0`, `SL` vs `OPEN`), the columns landed at different x-positions on every
row, and there was **no header**, so nothing established consistent column tracks.

---

## Fix

Replaced the flex layout with a **CSS Grid** shared by a header row and every body row, so all
columns line up on fixed tracks. Numeric columns are right-aligned; the side badge is centered
with a fixed min-width; under 700px each row collapses into a compact label/value card.

### Layout

| # | Column | Track |
|---|--------|-------|
| 1 | Symbol + timeframe | `minmax(180px, 2fr)` |
| 2 | Side (badge) | `120px` |
| 3 | Confidence | `120px` (right) |
| 4 | RR | `120px` (right) |
| 5 | Status | `120px` (right) |

The header (`.setup-row.setup-head`) uses the **exact same grid**, guaranteeing header↔body
alignment.

### CSS (key rules)
```css
.setup-table{width:100%}
.setup-row{display:grid;grid-template-columns:minmax(180px,2fr) 120px 120px 120px 120px;
           align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid rgba(80,140,200,.18)}
.setup-row>div{min-width:0}
.setup-row .symbol{font-weight:800;white-space:nowrap}
.setup-row .symbol span{color:#8ab4e6;font-size:12px;margin-left:4px}
.setup-row .conf,.setup-row .rr,.setup-row .status{text-align:right;font-weight:800}
.setup-row .badge{display:inline-flex;justify-content:center;align-items:center;min-width:56px}
.setup-head>div:nth-child(3),:nth-child(4),:nth-child(5){text-align:right}  /* header matches body */
```

### Mobile (`max-width:700px`)
```css
@media(max-width:700px){
  .setup-head{display:none}
  .setup-row{grid-template-columns:1fr;background:#0a111a;border:1px solid #17314b;border-radius:10px;padding:14px}
  .setup-row>div{display:flex;justify-content:space-between}
  .setup-row>div::before{content:attr(data-label);color:#7fa0c8;font-size:11px;text-transform:uppercase}
}
```
Each body cell carries a `data-label` (Symbol/Side/Confidence/RR/Status) so the card shows
label↔value pairs with no horizontal overflow.

### HTML/JS structure (rendered)
```html
<div class="setup-table">
  <div class="setup-row setup-head">
    <div>Symbol</div><div>Side</div><div>Confidence</div><div>RR</div><div>Status</div>
  </div>
  <div class="setup-row">
    <div class="symbol" data-label="Symbol">PRLUSDT <span>15m</span></div>
    <div data-label="Side"><span class="badge long">LONG</span></div>
    <div class="conf" data-label="Confidence">89%</div>
    <div class="rr" data-label="RR">1:2.29</div>
    <div class="status sl" data-label="Status">SL</div>
  </div>
</div>
```
Status maps to colour: `SL`→red, `OPEN`→cyan, TP-states→green (unchanged semantics).

---

## Files changed
| File | Change |
|------|--------|
| `app/dashboard/server.py` | `_market_radar_page_html()` — replaced `.mr-setup-row` flex CSS with the `.setup-table`/`.setup-row` grid + mobile card CSS; rebuilt the setups renderer to emit a header row and grid cells with `data-label`s. |

No other files touched. Trading logic, backend calculations and APIs are unchanged.

---

## Validation
- `python3 -c "import ast; ast.parse(...)"` → `server.py parse OK`
- `import app.dashboard.server` → OK
- Rendered `_market_radar_page_html()` contains the new grid CSS, the shared
  `grid-template-columns: minmax(180px,2fr) 120px 120px 120px 120px`, the header row,
  per-cell `data-label`s, and the `@media(max-width:700px)` card rules.
- Desktop: header and all body rows share one grid → LONG badges, confidence, RR all line
  up vertically; status is right-aligned.
- Mobile (<700px): rows render as compact label/value cards with no horizontal overflow.

> Live screenshots require the running dashboard + `/api/public/market-radar` data feed, which
> is not reachable from the sandbox; alignment is verified structurally (single shared grid
> for header and body) and by rendering the page HTML.
