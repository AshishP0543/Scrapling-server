# Scrapling Dashboard

A self-contained web UI to explore every power of Scrapling — fetching, parsing, and
AI-powered extraction — from the browser. No web framework, no AI SDK: the backend is
pure Python standard library wrapping Scrapling, and Gemini is called over its REST API.

## Run

```bash
./dashboard/run.sh
# or:  /usr/bin/python3.10 dashboard/server.py
```

Then open **http://127.0.0.1:8770**.

> The full Scrapling stack (lxml, curl_cffi, playwright, patchright + browsers) is
> installed under **`/usr/bin/python3.10`** on this machine, so the launcher uses it.
> Override with `SCRAPLING_PY=/path/to/python ./dashboard/run.sh`.
> Change host/port with `SCRAPLING_DASH_HOST` / `SCRAPLING_DASH_PORT`.

## Tabs

| Tab | What it does |
|-----|--------------|
| 🌐 **Fetch & Explore** | Fire a request with one of three engines and inspect status, headers, title, and the rendered text / raw HTML. |
| 🎯 **Selector Playground** | Run CSS / XPath / regex / `find_by_text` / `find_all` against the last fetched page (or pasted HTML) and view matches as text, outer HTML, or an attribute. |
| 🖼️ **Media & Assets** | One click pulls every **image** (visual thumbnail gallery), **link**, **video/audio/iframe**, **script** and **stylesheet** off the page — all URLs resolved to absolute — plus page metadata (title, description, OG tags, canonical). Catches lazy-load attrs, `srcset`, `<noscript>`, CSS backgrounds, JSON-LD, and **deep-scans inline JSON** for SPA galleries (e.g. Myntra). Copy image URLs or download the whole set as JSON. |
| 🎨 **Brand Identity** | Reads the site's CSS + DOM for the **color palette**, **brand color tokens** (CSS vars), **gradients** (rendered live), **fonts** (real `@font-face` samples via the brand's woff2 files), **logos** and **textures** — then optionally asks **Gemini** for the brand story, tone, current **campaigns**, **offers + dates**, and **new arrivals**. External stylesheets are fetched and scanned too. |
| 🤖 **AI Extract** | Convert the fetched page to markdown and ask **Gemini** to return structured JSON (presets for quotes, products, articles, contacts, tables). |
| ⚙️ **API Keys** | Store Gemini / OpenAI / Anthropic keys (Gemini is the active engine). Keys live only in the browser's `localStorage` and are sent straight to the provider. |

## Fetcher engines

- **Basic** — `Fetcher` (curl_cffi): method, browser impersonation, timeout, redirects,
  stealthy headers, HTTP/3, custom headers, POST body, proxy.
- **Stealthy** — `StealthyFetcher`: real anti-bot browser with `solve_cloudflare`,
  `block_webrtc`, `hide_canvas`, network-idle, ad blocking, etc.
- **Dynamic** — `DynamicFetcher` (Playwright): full JS rendering, `wait_selector`,
  extra wait, resource blocking, proxy.

## API (POST JSON)

- `POST /api/fetch` → `{url, fetcher, options}` → page + `doc_id`
- `POST /api/parse` → `{doc_id|html, selector_type, query, mode, attr}` → matches
- `POST /api/assets` → `{doc_id|html}` → `{images, links, media, scripts, styles, meta, counts}`
- `POST /api/brand`  → `{doc_id|html, gemini_key?}` → `{colors, css_vars, gradients, fonts, font_files, logos, textures, theme, ai}`
- `POST /api/ai`    → `{gemini_key, model, instruction, doc_id|content}` → JSON
- `GET  /api/capabilities` → which engines / browsers / AI are available

## Security notes

- AI keys are never written to disk by the server; they ride along with each AI request.
- Fetched documents are cached in memory only (last 25), keyed by `doc_id`.
- Intended for local use — it binds to `127.0.0.1` by default.
