#!/usr/bin/env python3.10
"""
Scrapling Scraping API — a clean, versioned REST service for structured scraping.

It reuses the dashboard's engine code (server.py) as a single source of truth and
exposes purpose-built endpoints:

    GET  /                 → self-documenting JSON index
    GET  /v1/health        → status + engine capabilities
    POST /v1/scrape        → fetch a page + run named CSS selectors
    POST /v1/product       → structured product data (JSON-LD → OG → heuristics → Gemini)
    POST /v1/brand         → full brand identity (colors, fonts, logos, gradients, AI story)
    POST /v1/assets        → all media/assets (images, links, video, scripts, styles)
    POST /v1/extract       → free-form AI extraction via Gemini

Every POST accepts either:
    {"url": "...", "fetcher": "basic|stealthy|dynamic", "options": {...}}
  or
    {"html": "<...>", "source_url": "https://..."}    # parse pre-fetched HTML

Run:  /usr/bin/python3.10 dashboard/api_server.py   (or ./dashboard/api.sh)
Default: http://127.0.0.1:8771   (override SCRAPLING_API_HOST / SCRAPLING_API_PORT)
"""
import os
import re
import sys
import hmac
import json
import time
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # the dashboard engine layer (do_fetch, do_brand, do_assets, do_ai, …)
from server import (  # noqa: E402
    Selector, Fetcher, DOCS, capabilities, raw_html, page_to_markdown,
    call_gemini, _abs, _dedupe, _best_img_from_attrs,
)

HOST = os.environ.get("SCRAPLING_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("SCRAPLING_API_PORT", "8771"))
# Shared-secret token. When set, every POST (the fetch surface) must present it via
# an `X-API-Token` header or a `?token=` query param. Empty → auth disabled (local only).
TOKEN = os.environ.get("SCRAPLING_API_TOKEN", "").strip()


# ---------------------------------------------------------------------------
# Shared: resolve request → a parsed Selector (fetch if a URL was given)
# ---------------------------------------------------------------------------
def resolve_sel(payload):
    """Return (selector, fetch_meta). fetch_meta is None when html was supplied."""
    if payload.get("url"):
        meta = server.do_fetch(payload)
        return DOCS[meta["doc_id"]], meta
    if payload.get("html"):
        return Selector(content=payload["html"], url=payload.get("source_url", "")), None
    raise ValueError("Provide either 'url' (to fetch) or 'html' (to parse).")


def fetch_meta(meta):
    if not meta:
        return {"mode": "html", "status": None}
    return {
        "status": meta.get("status"), "final_url": meta.get("final_url"),
        "title": meta.get("title"), "size": meta.get("size"),
    }


# ---------------------------------------------------------------------------
# JSON-LD helpers
# ---------------------------------------------------------------------------
def jsonld_nodes(sel):
    nodes = []
    for s in sel.css('script[type="application/ld+json"]'):
        try:
            txt = s.text or str(s.html_content)
        except Exception:
            continue
        if not txt:
            continue
        try:
            data = json.loads(txt)
        except Exception:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                graph = x.get("@graph")
                if graph:
                    stack.extend(graph if isinstance(graph, list) else [graph])
                nodes.append(x)
    return nodes


def is_type(node, t):
    ty = node.get("@type")
    if isinstance(ty, list):
        return any(str(s).lower() == t.lower() for s in ty)
    return str(ty).lower() == t.lower()


# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------
PRICE_RE = re.compile(r"(₹|Rs\.?|INR|\$|US\$|€|£|AED|SAR)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)


def _imgs_from(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v if isinstance(v, str) else (v.get("url") if isinstance(v, dict) else None) for v in value]
    if isinstance(value, dict):
        return [value.get("url")]
    return []


def extract_product(sel):
    p = {
        "name": None, "brand": None, "description": None, "category": None,
        "price": None, "currency": None, "availability": None,
        "sku": None, "gtin": None, "mpn": None,
        "rating": None, "review_count": None,
        "images": [], "breadcrumbs": [], "url": getattr(sel, "url", ""),
        "sources": [],
    }
    nodes = jsonld_nodes(sel)
    prod = next((n for n in nodes if is_type(n, "Product")), None)
    if prod:
        p["sources"].append("json-ld")
        p["name"] = prod.get("name")
        p["description"] = prod.get("description")
        p["sku"] = prod.get("sku")
        p["mpn"] = prod.get("mpn")
        p["gtin"] = prod.get("gtin13") or prod.get("gtin12") or prod.get("gtin")
        br = prod.get("brand")
        p["brand"] = br.get("name") if isinstance(br, dict) else br
        cat = prod.get("category")
        p["category"] = cat if isinstance(cat, str) else None
        p["images"] = _imgs_from(prod.get("image"))
        offers = prod.get("offers")
        off = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(off, dict):
            spec = off.get("priceSpecification") or {}
            spec = spec[0] if isinstance(spec, list) and spec else spec
            p["price"] = off.get("price") or (spec.get("price") if isinstance(spec, dict) else None)
            p["currency"] = off.get("priceCurrency") or (spec.get("priceCurrency") if isinstance(spec, dict) else None)
            av = off.get("availability")
            p["availability"] = av.split("/")[-1] if isinstance(av, str) else None
        agg = prod.get("aggregateRating")
        if isinstance(agg, dict):
            p["rating"] = agg.get("ratingValue")
            p["review_count"] = agg.get("reviewCount") or agg.get("ratingCount")

    # OpenGraph / meta fallback
    def og(*props):
        for pr in props:
            v = sel.css(f'meta[property="{pr}"]::attr(content)').get() or \
                sel.css(f'meta[name="{pr}"]::attr(content)').get()
            if v:
                return v
        return None

    if not p["name"]:
        p["name"] = og("og:title") or sel.css("h1::text").get() or sel.css("title::text").get()
        if p["name"]:
            p["sources"].append("og/heuristic")
    if not p["description"]:
        p["description"] = og("og:description", "description", "twitter:description")
    if not p["brand"]:
        p["brand"] = og("og:brand", "product:brand", "og:site_name")
    if not p["images"]:
        oi = og("og:image", "og:image:url", "twitter:image")
        if oi:
            p["images"] = [oi]
    if p["price"] in (None, ""):
        p["price"] = og("product:price:amount", "og:price:amount")
    if not p["currency"]:
        p["currency"] = og("product:price:currency", "og:price:currency")

    # breadcrumbs from JSON-LD
    bc = next((n for n in nodes if is_type(n, "BreadcrumbList")), None)
    if bc:
        items = bc.get("itemListElement", [])
        p["breadcrumbs"] = [
            (it.get("name") or (it.get("item") or {}).get("name"))
            for it in items if isinstance(it, dict)
        ]
        p["breadcrumbs"] = [b for b in p["breadcrumbs"] if b]

    # price heuristic from visible text
    if p["price"] in (None, ""):
        try:
            text = str(sel.get_all_text(strip=True))[:20000]
        except Exception:
            text = ""
        m = PRICE_RE.search(text)
        if m:
            p["currency"] = p["currency"] or m.group(1).upper()
            p["price"] = m.group(2).replace(",", "")
            p["sources"].append("price-regex")

    # images: resolve + deep-scan the page source when sparse (SPA galleries)
    imgs = [_abs(sel, i) for i in p["images"] if i]
    if len(imgs) < 2:
        for img in sel.css("img"):
            u = _best_img_from_attrs(img.attrib)
            if u and not u.startswith("data:image/svg"):
                imgs.append(_abs(sel, u))
        blob = raw_html(sel).replace("\\/", "/")
        for m in re.findall(r'https?://[^\s"\'\\<>(){}\[\]]+?\.(?:jpe?g|png|webp|avif)', blob, re.I)[:200]:
            imgs.append(m)
    p["images"] = _dedupe(imgs)[:40]

    if isinstance(p["price"], str):
        p["price"] = p["price"].replace(",", "").strip()
    return p


ENRICH_PROMPT = (
    "You are a product-data extraction engine. Given a product page's markdown and the "
    "fields already extracted, fill in what's missing and add structured detail. "
    "Do NOT contradict non-null extracted values.\n\n"
    "ALREADY EXTRACTED:\n{known}\n\n"
    "Return ONLY valid JSON:\n"
    "{{\n"
    '  "name": str, "brand": str, "price": str, "currency": str, "description": str,\n'
    '  "highlights": [str], "specifications": {{"key": "value"}},\n'
    '  "variants": [{{"type": str, "value": str}}], "category": str,\n'
    '  "in_stock": bool\n'
    "}}\n\nPAGE CONTENT:\n{content}"
)


def enrich_product(sel, product, payload):
    known = {k: v for k, v in product.items() if v not in (None, [], "")}
    md = page_to_markdown(sel)[:50000]
    prompt = ENRICH_PROMPT.format(known=json.dumps(known)[:4000], content=md)
    txt = call_gemini(payload["gemini_key"], payload.get("model") or "gemini-2.5-flash", prompt, json_mode=True)
    try:
        ai = json.loads(txt)
    except Exception:
        product["ai_raw"] = txt
        return product
    # fill blanks from AI, keep deterministic values
    for k in ("name", "brand", "price", "currency", "description", "category"):
        if product.get(k) in (None, "", []) and ai.get(k) not in (None, ""):
            product[k] = ai[k]
    product["highlights"] = ai.get("highlights") or []
    product["specifications"] = ai.get("specifications") or {}
    product["variants"] = ai.get("variants") or []
    if ai.get("in_stock") is not None and not product.get("availability"):
        product["availability"] = "InStock" if ai["in_stock"] else "OutOfStock"
    product["sources"].append("gemini")
    return product


# ---------------------------------------------------------------------------
# Endpoint handlers (each returns the `data` payload)
# ---------------------------------------------------------------------------
def ep_scrape(payload):
    sel, meta = resolve_sel(payload)
    selectors = payload.get("selectors") or {}
    extracted = {}
    for name, q in selectors.items():
        try:
            vals = [str(x.get_all_text(strip=True)) if hasattr(x, "get_all_text") else str(x) for x in sel.css(q)]
            extracted[name] = vals[0] if len(vals) == 1 else vals
        except Exception as e:
            extracted[name] = {"error": str(e)}
    out = {"fetch": fetch_meta(meta), "url": getattr(sel, "url", ""), "extracted": extracted}
    if payload.get("include_text"):
        out["text"] = str(sel.get_all_text(strip=True))[:20000]
    return out


def ep_product(payload):
    sel, meta = resolve_sel(payload)
    product = extract_product(sel)
    if payload.get("enrich") and payload.get("gemini_key"):
        try:
            product = enrich_product(sel, product, payload)
        except Exception as e:
            product["enrich_error"] = str(e)
    return {"fetch": fetch_meta(meta), "product": product}


def ep_brand(payload):
    sel, meta = resolve_sel(payload)
    doc_id = next((k for k, v in DOCS.items() if v is sel), None)
    inner = {"doc_id": doc_id} if doc_id else {"html": payload.get("html", ""), "url": payload.get("source_url", "")}
    inner["gemini_key"] = payload.get("gemini_key", "")
    inner["model"] = payload.get("model")
    return {"fetch": fetch_meta(meta), "brand": server.do_brand(inner)}


def ep_assets(payload):
    sel, meta = resolve_sel(payload)
    doc_id = next((k for k, v in DOCS.items() if v is sel), None)
    inner = {"doc_id": doc_id} if doc_id else {"html": payload.get("html", ""), "url": payload.get("source_url", "")}
    return {"fetch": fetch_meta(meta), "assets": server.do_assets(inner)}


def ep_extract(payload):
    sel, meta = resolve_sel(payload)
    doc_id = next((k for k, v in DOCS.items() if v is sel), None)
    inner = {
        "doc_id": doc_id, "gemini_key": payload.get("gemini_key", ""),
        "model": payload.get("model"), "instruction": payload.get("instruction", ""),
        "content": payload.get("content", ""),
    }
    return {"fetch": fetch_meta(meta), "result": server.do_ai(inner)}


ROUTES = {
    "/v1/scrape": ep_scrape,
    "/v1/product": ep_product,
    "/v1/brand": ep_brand,
    "/v1/assets": ep_assets,
    "/v1/extract": ep_extract,
}

API_INDEX = {
    "service": "scrapling-scraping-api",
    "version": "1",
    "engines": "basic | stealthy | dynamic",
    "endpoints": {
        "GET /v1/health": "status + engine capabilities",
        "POST /v1/scrape": "fetch + named CSS selectors  {url|html, fetcher?, options?, selectors:{name:css}}",
        "POST /v1/product": "structured product data      {url|html, fetcher?, enrich?, gemini_key?}",
        "POST /v1/brand": "brand identity                 {url|html, fetcher?, gemini_key?}",
        "POST /v1/assets": "all media/assets              {url|html, fetcher?}",
        "POST /v1/extract": "free-form AI extraction       {url|html, instruction, gemini_key}",
    },
    "body": "POST {url:'https://…', fetcher:'stealthy', options:{network_idle:true,wait:'2000'}}  OR  {html:'<…>', source_url:'…'}",
}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "ScraplingAPI/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[api] " + (fmt % args) + "\n")

    def _send(self, code, body):
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        """True when auth is disabled or a correct token is presented."""
        if not TOKEN:
            return True
        supplied = self.headers.get("X-API-Token", "")
        if not supplied:
            from urllib.parse import urlparse, parse_qs
            supplied = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        return hmac.compare_digest(supplied, TOKEN)

    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/v1", "/v1/"):
            return self._send(200, API_INDEX)
        if path == "/v1/health":
            return self._send(200, {"status": "ok", "capabilities": capabilities()})
        return self._send(404, {"ok": False, "error": "not found", "see": "/"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"ok": False, "error": "invalid or missing API token",
                                    "hint": "send header 'X-API-Token: <token>' or ?token=<token>"})
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if not handler:
            return self._send(404, {"ok": False, "error": f"unknown endpoint {path}", "see": "/"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send(400, {"ok": False, "error": f"bad JSON body: {e}"})
        t0 = time.time()
        try:
            data = handler(payload)
            return self._send(200, {"ok": True, "data": data,
                                    "meta": {"elapsed_ms": int((time.time() - t0) * 1000)}})
        except Exception as e:
            traceback.print_exc()
            return self._send(200, {"ok": False, "error": str(e), "type": type(e).__name__,
                                    "meta": {"elapsed_ms": int((time.time() - t0) * 1000)}})


def main():
    caps = capabilities()
    print("=" * 64)
    print(" Scrapling Scraping API")
    print(f"   url      : http://{HOST}:{PORT}   (docs at /)")
    print(f"   auth     : {'ON  (X-API-Token required on POST)' if TOKEN else 'OFF — set SCRAPLING_API_TOKEN before exposing publicly!'}")
    print(f"   engines  : basic=on stealthy={'on' if caps['stealthy'] else 'off'} "
          f"dynamic={'on' if caps['dynamic'] else 'off'} browsers={'on' if caps['browsers'] else 'off'}")
    print("   routes   : /v1/scrape /v1/product /v1/brand /v1/assets /v1/extract")
    print("=" * 64)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
