# Scrapling Scraping API

A clean, versioned REST service for structured scraping, built on Scrapling. It reuses
the dashboard's engine layer (`server.py`) so the UI and the API share one code path.

## Run

```bash
./dashboard/api.sh            # → http://127.0.0.1:8771
# or: /usr/bin/python3.10 dashboard/api_server.py
# env: SCRAPLING_API_HOST, SCRAPLING_API_PORT, SCRAPLING_PY
```

The dashboard UI (`server.py`, port 8770) and this API (port 8771) run independently —
start either or both.

## Request shape
https://code.visualstudio.com/docs/languages/markdown
Every `POST` accepts **either** a URL to fetch **or** pre-fetched HTML:

```jsonc
{ "url": "https://…", "fetcher": "basic|stealthy|dynamic", "options": { ... } }
// or
{ "html": "<html>…</html>", "source_url": "https://…" }
```

`fetcher` defaults to `basic`. Use **`stealthy`** for anti-bot stores (Myntra, etc.) and
**`dynamic`** for JS-rendered pages. `options` are passed straight to the engine
(`network_idle`, `wait`, `wait_selector`, `timeout`, `impersonate`, `proxy`, …).

AI fields (`gemini_key`, `model`, `enrich`, `instruction`) are optional and only used by
the endpoints that mention them.

## Response envelope

```jsonc
{ "ok": true, "data": { … }, "meta": { "elapsed_ms": 543 } }
// errors:
{ "ok": false, "error": "…", "type": "ValueError", "meta": { "elapsed_ms": 12 } }
```

## Endpoints

| Method & path | Purpose |
|---|---|
| `GET /` | Self-documenting JSON index |
| `GET /v1/health` | Status + engine capabilities |
| `POST /v1/scrape` | Fetch + run named CSS selectors |
| `POST /v1/product` | Structured product data |
| `POST /v1/brand` | Full brand identity |
| `POST /v1/assets` | All media / assets |
| `POST /v1/extract` | Free-form AI extraction (Gemini) |

### `POST /v1/product`
JSON-LD `Product` schema → OpenGraph → visible-text/price heuristics → optional Gemini.
Set `"enrich": true` + `"gemini_key"` to fill gaps and add highlights/specs/variants.

```bash
curl -s localhost:8771/v1/product -H 'Content-Type: application/json' -d '{
  "url": "https://www.myntra.com/.../buy",
  "fetcher": "stealthy",
  "options": {"network_idle": true, "wait": "3000"},
  "enrich": true, "gemini_key": "AIza…"
}'
```
Returns `product`: `name, brand, description, category, price, currency, availability,
sku, gtin, mpn, rating, review_count, images[], breadcrumbs[], sources[]`
(+ `highlights, specifications, variants` when enriched).

### `POST /v1/scrape`
```bash
curl -s localhost:8771/v1/scrape -H 'Content-Type: application/json' -d '{
  "url": "https://quotes.toscrape.com",
  "selectors": {"quotes": ".quote .text::text", "authors": ".author::text"},
  "include_text": false
}'
```
Returns `extracted`: a map of your selector names → value (string for one match, array
for many).

### `POST /v1/brand`
Returns `brand`: `colors[], css_vars[], gradients[], fonts[], font_files[], logos[],
textures[], theme{}`, plus `ai{}` (brand story, campaigns, offers, new arrivals) when a
`gemini_key` is supplied. External stylesheets are fetched and scanned.

### `POST /v1/assets`
Returns `assets`: `images[], links[], media[], scripts[], styles[], meta{}, counts{}` —
images include lazy-load attrs, `srcset`, `<noscript>`, CSS backgrounds, JSON-LD, and an
inline-JSON deep scan for SPA galleries.

### `POST /v1/extract`
```bash
curl -s localhost:8771/v1/extract -H 'Content-Type: application/json' -d '{
  "url": "https://…", "instruction": "Extract all products as JSON",
  "gemini_key": "AIza…", "model": "gemini-2.5-flash"
}'
```

## Notes
- CORS is open (`Access-Control-Allow-Origin: *`) so you can call it from a browser app.
- Threaded server; browser engines (`stealthy`/`dynamic`) take ~10–40s per request.
- Gemini keys are passed per-request and never written to disk.
