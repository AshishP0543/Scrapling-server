#!/usr/bin/env python3.10
"""
Scrapling Dashboard — a zero-dependency web UI to explore every power of Scrapling:
fetching (basic / stealthy / dynamic browser), CSS/XPath/regex/text selectors, and
AI-powered structured extraction via Google Gemini.

Run:  /usr/bin/python3.10 dashboard/server.py  (or ./dashboard/run.sh)
Then open http://127.0.0.1:8770

The server only wraps Scrapling + the standard library. Gemini is called directly
over its public REST API, so no extra SDK is required. AI keys never touch disk:
the browser keeps them in localStorage and sends the Gemini key with each AI request.
"""
import json
import os
import re
import sys
import uuid
import traceback
import urllib.request
import urllib.error
from collections import OrderedDict
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("SCRAPLING_DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("SCRAPLING_DASH_PORT", "8770"))

# --- Scrapling core (parser + basic fetcher always available) -----------------
from scrapling import Selector, Fetcher  # noqa: E402

try:
    from markdownify import markdownify as _md
except Exception:  # pragma: no cover
    _md = None

# In-memory cache of fetched documents so the playground / AI can reference a page
# without round-tripping the full HTML every call.
DOCS: "OrderedDict[str, object]" = OrderedDict()
MAX_DOCS = 25

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
)


def store_doc(resp) -> str:
    doc_id = uuid.uuid4().hex[:12]
    DOCS[doc_id] = resp
    while len(DOCS) > MAX_DOCS:
        DOCS.popitem(last=False)
    return doc_id


# --- helpers ------------------------------------------------------------------
def _to_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _clean(d):
    """Drop empty-string / None values so we only pass real options to Scrapling."""
    return {k: v for k, v in d.items() if v not in ("", None)}


def raw_html(resp):
    try:
        body = resp.body
        if isinstance(body, (bytes, bytearray)):
            return body.decode("utf-8", "replace")
        if body:
            return str(body)
    except Exception:
        pass
    try:
        return str(resp.html_content)
    except Exception:
        return ""


def page_to_markdown(resp):
    html = raw_html(resp)
    if _md and html:
        try:
            return _md(html, strip=["script", "style"], heading_style="ATX")
        except Exception:
            pass
    try:
        return str(resp.get_all_text(strip=True))
    except Exception:
        return html


# --- fetching -----------------------------------------------------------------
def build_basic_kwargs(opts):
    kw = {}
    imp = opts.get("impersonate")
    if imp and imp != "none":
        kw["impersonate"] = imp
    if opts.get("timeout"):
        kw["timeout"] = float(opts["timeout"])
    if "follow_redirects" in opts:
        kw["follow_redirects"] = _to_bool(opts["follow_redirects"])
    if "stealthy_headers" in opts:
        kw["stealthy_headers"] = _to_bool(opts["stealthy_headers"])
    if _to_bool(opts.get("http3", False)):
        kw["http3"] = True
    if opts.get("proxy"):
        kw["proxy"] = opts["proxy"]
    for field in ("headers", "params", "cookies"):
        val = opts.get(field)
        if isinstance(val, dict) and val:
            kw[field] = val
    return kw


def build_browser_kwargs(opts):
    kw = {}
    bool_flags = (
        "headless", "network_idle", "disable_resources", "block_ads",
        "google_search", "real_chrome", "solve_cloudflare", "block_webrtc",
        "hide_canvas", "load_dom", "dns_over_https",
    )
    for f in bool_flags:
        if f in opts:
            kw[f] = _to_bool(opts[f])
    if opts.get("wait_selector"):
        kw["wait_selector"] = opts["wait_selector"]
    if opts.get("timeout"):
        kw["timeout"] = int(float(opts["timeout"]))
    if opts.get("wait"):
        kw["wait"] = int(float(opts["wait"]))
    if opts.get("useragent"):
        kw["useragent"] = opts["useragent"]
    if opts.get("proxy"):
        kw["proxy"] = opts["proxy"]
    return kw


def do_fetch(payload):
    url = (payload.get("url") or "").strip()
    if not url:
        raise ValueError("A URL is required.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    fetcher = payload.get("fetcher", "basic")
    opts = payload.get("options", {}) or {}

    if fetcher == "basic":
        method = (opts.get("method", "GET") or "GET").upper()
        kwargs = build_basic_kwargs(opts)
        body = opts.get("body")
        if method in ("POST", "PUT", "DELETE") and body:
            try:
                kwargs["json"] = json.loads(body)
            except Exception:
                kwargs["data"] = body
        fn = getattr(Fetcher, method.lower(), Fetcher.get)
        resp = fn(url, **kwargs)
    elif fetcher == "stealthy":
        from scrapling import StealthyFetcher
        resp = StealthyFetcher.fetch(url, **build_browser_kwargs(opts))
    elif fetcher == "dynamic":
        from scrapling import DynamicFetcher
        resp = DynamicFetcher.fetch(url, **build_browser_kwargs(opts))
    else:
        raise ValueError(f"Unknown fetcher: {fetcher}")

    doc_id = store_doc(resp)
    html = raw_html(resp)
    try:
        headers = dict(resp.headers)
    except Exception:
        headers = {}
    try:
        title = resp.css("title::text").get()
    except Exception:
        title = None
    return {
        "doc_id": doc_id,
        "status": getattr(resp, "status", None),
        "reason": getattr(resp, "reason", None),
        "final_url": getattr(resp, "url", url),
        "title": title,
        "headers": headers,
        "html": html,
        "size": len(html),
        "encoding": getattr(resp, "encoding", None),
    }


# --- parsing / selector playground -------------------------------------------
def _extract(el, mode, attr):
    if mode == "attr":
        try:
            return el.attrib.get(attr)
        except Exception:
            return None
    if mode in ("html", "element"):
        try:
            return str(el.get())
        except Exception:
            return str(el)
    # default: text
    try:
        return str(el.get_all_text(strip=True))
    except Exception:
        return str(el)


def do_parse(payload):
    doc_id = payload.get("doc_id")
    if doc_id and doc_id in DOCS:
        sel = DOCS[doc_id]
    elif payload.get("html"):
        sel = Selector(content=payload["html"])
    else:
        raise ValueError("No document to parse. Fetch a page first or paste HTML.")

    stype = payload.get("selector_type", "css")
    query = payload.get("query", "")
    mode = payload.get("mode", "text")
    attr = payload.get("attr", "")
    limit = int(payload.get("limit", 100))

    if stype == "regex":
        matches = [str(m) for m in sel.re(query)]
        return {"results": matches[:limit], "count": len(matches), "selector_type": stype}

    if stype == "css":
        items = sel.css(query)
    elif stype == "xpath":
        items = sel.xpath(query)
    elif stype == "find_by_text":
        found = sel.find_by_text(query, first_match=False, partial=True)
        items = found if found else []
    elif stype == "find_all":
        items = sel.find_all(query) if query else sel.find_all()
    else:
        raise ValueError(f"Unknown selector type: {stype}")

    count = len(items)
    results = [_extract(el, mode, attr) for el in items[:limit]]
    return {"results": results, "count": count, "selector_type": stype}


# --- media / asset extraction -------------------------------------------------
def _abs(sel, u):
    if not u:
        return u
    u = str(u).strip()
    if u.startswith(("http://", "https://", "data:", "//")):
        return ("https:" + u) if u.startswith("//") else u
    try:
        return sel.urljoin(u)
    except Exception:
        return u


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        key = x if isinstance(x, str) else x.get("url") or x.get("href") or x.get("src")
        if key and key not in seen:
            seen.add(key)
            out.append(x)
    return out


IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".jfif", ".ico")
# attributes stores stash the real image URL in, to dodge scrapers / lazy-load
LAZY_ATTRS = (
    "src", "data-src", "data-srcset", "srcset", "data-lazy", "data-lazy-src",
    "data-original", "data-original-src", "data-image", "data-img", "data-zoom-image",
    "data-large_image", "data-large-image", "data-hi-res-src", "data-old-hires",
    "data-thumb", "data-thumbnail", "data-flickity-lazyload", "data-flickity-lazyload-src",
)
_PLACEHOLDER = ("placeholder", "blank.", "spacer", "1x1", "pixel", "loading", "lazyload-")


def _is_img_url(u):
    if not u:
        return False
    u = u.lower().split("?")[0]
    return u.startswith("data:image") or any(u.endswith(e) for e in IMG_EXT) or any(e + "/" in u for e in IMG_EXT)


def _srcset_urls(srcset):
    out = []
    for part in (srcset or "").split(","):
        part = part.strip()
        if part:
            out.append(part.split()[0])
    return out


def _srcset_first(srcset):
    u = _srcset_urls(srcset)
    return u[0] if u else ""


def _is_placeholder(u):
    lo = u.lower()
    return lo.startswith("data:image/svg") or any(p in lo for p in _PLACEHOLDER)


def _best_img_from_attrs(a):
    """Pull the real image URL out of an <img>/<source>'s many possible attributes."""
    cands = []
    for attr in LAZY_ATTRS:
        v = a.get(attr)
        if not v:
            continue
        if "srcset" in attr:
            cands += _srcset_urls(v)
        else:
            cands.append(v)
    # Amazon-style {"url": [w,h], ...}
    dyn = a.get("data-a-dynamic-image")
    if dyn:
        try:
            cands += list(json.loads(dyn).keys())
        except Exception:
            pass
    cands = [c.strip() for c in cands if c and c.strip()]
    # prefer a real, non-placeholder candidate
    for c in cands:
        if not _is_placeholder(c):
            return c
    return cands[0] if cands else None


def _json_ld_images(sel):
    urls = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k.lower() == "image":
                    if isinstance(v, str):
                        urls.append(v)
                    elif isinstance(v, list):
                        urls += [x if isinstance(x, str) else x.get("url") for x in v if x]
                    elif isinstance(v, dict) and v.get("url"):
                        urls.append(v["url"])
                else:
                    walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    for s in sel.css('script[type="application/ld+json"]'):
        txt = None
        try:
            txt = s.text or str(s.html_content)
        except Exception:
            pass
        if not txt:
            continue
        try:
            walk(json.loads(txt))
        except Exception:
            pass
    return [u for u in urls if u]


def _get_sel(payload):
    doc_id = payload.get("doc_id")
    if doc_id and doc_id in DOCS:
        return DOCS[doc_id]
    if payload.get("html"):
        return Selector(content=payload["html"], url=payload.get("url", ""))
    raise ValueError("No document. Fetch a page first or paste HTML.")


def do_assets(payload):
    sel = _get_sel(payload)

    # --- images: hunt every lazy-load / product-image pattern ---
    images = []

    def add_img(u, alt="", kind="img"):
        if u and not u.startswith("data:image/svg"):
            images.append({"url": _abs(sel, u), "alt": (alt or "").strip(), "kind": kind})

    for img in sel.css("img"):
        a = img.attrib
        add_img(_best_img_from_attrs(a), a.get("alt") or a.get("title") or "", "img")
    for s in sel.css("source"):
        a = s.attrib
        add_img(_best_img_from_attrs(a) or _srcset_first(a.get("srcset", "")), "", "source")
    # JS-disabled fallbacks frequently carry the real product image
    for ns in sel.css("noscript"):
        try:
            inner = ns.text or str(ns.html_content)
            for im in Selector(content=str(inner), url=sel.url).css("img"):
                add_img(_best_img_from_attrs(im.attrib), im.attrib.get("alt", ""), "noscript")
        except Exception:
            pass
    # CSS background-image on inline styles
    for el in sel.css("[style*='background']"):
        for u in re.findall(r"url\(([^)]+)\)", el.attrib.get("style", "")):
            u = u.strip("'\" ")
            if _is_img_url(u):
                add_img(u, "", "bg")
    # structured data (schema.org Product etc.)
    for u in _json_ld_images(sel):
        add_img(u, "structured-data", "json-ld")
    # social preview + favicons
    for m in sel.css('meta[property="og:image"], meta[property="og:image:url"], meta[name="twitter:image"]'):
        add_img(m.attrib.get("content"), "social/preview", "og")
    for ic in sel.css('link[rel~="icon"], link[rel="apple-touch-icon"]'):
        add_img(ic.attrib.get("href"), "favicon", "icon")

    # deep scan: image URLs embedded in inline JSON / scripts (SPAs like Myntra,
    # where the product gallery lives in a JSON state blob, not in <img> tags).
    if payload.get("deep_scan", True):
        try:
            blob = raw_html(sel).replace("\\/", "/").replace("\\u002F", "/")
        except Exception:
            blob = ""
        found = 0
        for m in re.findall(r"""https?:(?://|\\?/\\?/)[^\s"'\\<>(){}\[\]]+?\.(?:jpe?g|png|webp|avif|gif)""", blob, re.I):
            add_img(m.replace("\\", ""), "", "embedded")
            found += 1
            if found >= 400:
                break
        # protocol-relative (//assets.myntassets.com/...jpg)
        for m in re.findall(r"""(?<![:/])//[a-z0-9.-]+/[^\s"'\\<>(){}\[\]]+?\.(?:jpe?g|png|webp|avif|gif)""", blob, re.I):
            add_img("https:" + m, "", "embedded")

    images = _dedupe(images)

    # --- links ---
    links = []
    for a in sel.css("a[href]"):
        href = a.attrib.get("href")
        if not href or href.startswith(("javascript:", "#")):
            continue
        try:
            text = str(a.get_all_text(strip=True))[:90]
        except Exception:
            text = ""
        links.append({"url": _abs(sel, href), "text": text})
    links = _dedupe(links)

    # --- media (video/audio/iframe/embed) ---
    media = []
    for tag in ("video", "audio"):
        for el in sel.css(tag):
            src = el.attrib.get("src")
            if src:
                media.append({"url": _abs(sel, src), "kind": tag})
            for so in el.css("source"):
                s2 = so.attrib.get("src")
                if s2:
                    media.append({"url": _abs(sel, s2), "kind": tag + "/source"})
    for fr in sel.css("iframe[src], embed[src]"):
        media.append({"url": _abs(sel, fr.attrib.get("src")), "kind": "iframe"})
    media = _dedupe(media)

    scripts = _dedupe([_abs(sel, s.attrib.get("src")) for s in sel.css("script[src]")])
    styles = _dedupe([_abs(sel, l.attrib.get("href")) for l in sel.css('link[rel="stylesheet"]')])

    def _meta(css):
        try:
            return sel.css(css).get()
        except Exception:
            return None

    meta = {
        "title": _meta("title::text"),
        "description": _meta('meta[name="description"]::attr(content)'),
        "og:title": _meta('meta[property="og:title"]::attr(content)'),
        "og:description": _meta('meta[property="og:description"]::attr(content)'),
        "og:type": _meta('meta[property="og:type"]::attr(content)'),
        "canonical": _abs(sel, _meta('link[rel="canonical"]::attr(href)')),
    }
    meta = {k: v for k, v in meta.items() if v}

    return {
        "images": images, "links": links, "media": media,
        "scripts": scripts, "styles": styles, "meta": meta,
        "counts": {
            "images": len(images), "links": len(links), "media": len(media),
            "scripts": len(scripts), "styles": len(styles),
        },
    }


# --- brand identity extraction ------------------------------------------------
GENERIC_FONTS = {
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "-apple-system", "blinkmacsystemfont", "ui-sans-serif", "ui-serif",
    "ui-monospace", "inherit", "initial", "unset", "none", "menu", "math",
    "emoji", "segoe ui", "roboto", "helvetica", "helvetica neue", "arial",
}


def _collect_css(sel, max_files=6, per_file=600000):
    """Gather CSS from <style> blocks, external stylesheets, and inline style attrs."""
    blocks, files = [], []
    for st in sel.css("style"):
        try:
            blocks.append(str(st.text or st.html_content))
        except Exception:
            pass
    hrefs = []
    for l in sel.css('link[rel="stylesheet"]'):
        h = l.attrib.get("href")
        if h:
            hrefs.append(_abs(sel, h))
    for h in _dedupe(hrefs)[:max_files]:
        if not h or h.startswith("data:"):
            continue
        try:
            r = Fetcher.get(h, timeout=8, stealthy_headers=True)
            body = r.body
            txt = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
            blocks.append(txt[:per_file])
            files.append(h)
        except Exception:
            pass
    inline = []
    for el in sel.css("[style]")[:600]:
        s = el.attrib.get("style")
        if s:
            inline.append(s)
    blocks.append("\n".join(inline))
    return "\n".join(blocks), files


def _norm_hex(h):
    h = h.lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    elif len(h) == 4:
        h = "".join(c * 2 for c in h[:3])
    if len(h) >= 6:
        return "#" + h[:6]
    return None


def extract_colors(css):
    from collections import Counter
    cnt = Counter()
    for h in re.findall(r"#([0-9a-fA-F]{3,8})\b", css):
        nh = _norm_hex(h)
        if nh:
            cnt[nh] += 1
    for r_, g_, b_ in re.findall(r"rgba?\(\s*(\d{1,3})[,\s]+(\d{1,3})[,\s]+(\d{1,3})", css):
        try:
            cnt["#%02x%02x%02x" % (int(r_), int(g_), int(b_))] += 1
        except Exception:
            pass
    return [{"hex": h, "count": c} for h, c in cnt.most_common(24)]


def extract_css_vars(css):
    seen = {}
    for name, val in re.findall(
        r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|hsla?\([^)]+\))", css
    ):
        seen[name] = val.strip()
    return [{"name": k, "value": v} for k, v in list(seen.items())[:48]]


def extract_gradients(css):
    pat = re.compile(
        r"(?:repeating-)?(?:linear|radial|conic)-gradient\((?:[^()]|\([^()]*\))*\)", re.I
    )
    return _dedupe([g.strip() for g in pat.findall(css)])[:18]


def extract_fonts(css, sel):
    from collections import Counter
    fam = Counter()
    for decl in re.findall(r"font-family\s*:\s*([^;{}]+)", css, re.I):
        first = decl.split(",")[0].strip().strip("'\"")
        if first and first.lower() not in GENERIC_FONTS and not first.startswith("var("):
            fam[first] += 1
    for ff in re.findall(r"@font-face\s*\{[^}]*\}", css, re.I):
        m = re.search(r"font-family\s*:\s*([^;]+)", ff, re.I)
        if m:
            f = m.group(1).strip().strip("'\"")
            if f and f.lower() not in GENERIC_FONTS:
                fam[f] += 1
    google = []
    for l in sel.css('link[href*="fonts.googleapis.com"]'):
        for f in re.findall(r"family=([^&:]+)", l.attrib.get("href", "")):
            google.append(f.replace("+", " "))
    for g in google:
        fam[g] += 2
    files = [
        _abs(sel, u.strip("'\" "))
        for u in re.findall(r"url\(([^)]+\.(?:woff2?|ttf|otf|eot))", css, re.I)
    ]
    fams = [{"name": n, "count": c} for n, c in fam.most_common(12)]
    return fams, _dedupe(files)[:12], _dedupe(google)


def extract_textures(css, sel):
    urls = []
    for u in re.findall(r"url\(([^)]+)\)", css):
        u = u.strip("'\" ")
        if re.search(r"\.(?:png|jpe?g|webp|avif|svg|gif)(?:\?|$)", u, re.I) and not u.startswith("data:"):
            urls.append(_abs(sel, u))
    return _dedupe(urls)[:24]


def extract_logos(sel):
    logos = []

    def addl(u, src):
        if u and not u.startswith("data:image/svg"):
            logos.append({"url": _abs(sel, u), "source": src})

    for img in sel.css("img"):
        a = img.attrib
        blob = " ".join(
            filter(None, [a.get("src", ""), a.get("alt", ""), a.get("class", ""), a.get("id", "")])
        ).lower()
        if "logo" in blob or "brand" in blob:
            addl(_best_img_from_attrs(a), "logo-img")
    for img in sel.css('header img, a[href="/"] img, [class*="logo"] img, [class*="brand"] img'):
        addl(_best_img_from_attrs(img.attrib), "header")
    for m in sel.css('meta[property="og:image"], meta[property="og:logo"]'):
        addl(m.attrib.get("content"), "og:image")
    for ic in sel.css('link[rel="apple-touch-icon"], link[rel~="icon"]'):
        addl(ic.attrib.get("href"), "icon")
    return _dedupe(logos)[:16]


def extract_theme(sel):
    out = {}
    for m in sel.css('meta[name="theme-color"]'):
        out["theme-color"] = m.attrib.get("content")
    for m in sel.css('meta[name="msapplication-TileColor"]'):
        out["tile-color"] = m.attrib.get("content")
    return {k: v for k, v in out.items() if v}


BRAND_PROMPT = (
    "You are a brand strategist analysing a company's web page. Using the page content "
    "below (and the design hints), infer the brand identity.\n\n"
    "DESIGN HINTS (extracted from the site's CSS): {hints}\n\n"
    "Return ONLY valid JSON with this shape (use null / [] when unknown, never invent):\n"
    "{{\n"
    '  "brand_name": str, "tagline": str, "brand_story": str (2-4 sentences),\n'
    '  "mission": str, "tone_of_voice": str, "target_audience": str,\n'
    '  "color_story": str (what the palette communicates),\n'
    '  "visual_style": str (textures, gradients, imagery vibe),\n'
    '  "current_campaigns": [{{"name": str, "description": str, "dates": str}}],\n'
    '  "offers": [{{"text": str, "code": str, "expires": str}}],\n'
    '  "new_arrivals": [{{"name": str, "detail": str}}],\n'
    '  "notable_dates": [str]\n'
    "}}\n\nPAGE CONTENT:\n{content}"
)


def do_brand(payload):
    sel = _get_sel(payload)
    css, css_files = _collect_css(sel)
    fams, font_files, google = extract_fonts(css, sel)
    result = {
        "colors": extract_colors(css),
        "css_vars": extract_css_vars(css),
        "gradients": extract_gradients(css),
        "theme": extract_theme(sel),
        "logos": extract_logos(sel),
        "textures": extract_textures(css, sel),
        "fonts": fams,
        "font_files": font_files,
        "google_fonts": google,
        "css_files": css_files,
    }
    key = (payload.get("gemini_key") or "").strip()
    if key:
        try:
            md = page_to_markdown(sel)[:60000]
            hints = "palette=%s; fonts=%s; gradients=%d" % (
                ", ".join(c["hex"] for c in result["colors"][:8]),
                ", ".join(f["name"] for f in fams[:5]) or "n/a",
                len(result["gradients"]),
            )
            prompt = BRAND_PROMPT.format(hints=hints, content=md)
            txt = call_gemini(key, payload.get("model") or "gemini-2.5-flash", prompt, json_mode=True)
            try:
                result["ai"] = json.loads(txt)
            except Exception:
                result["ai_raw"] = txt
        except Exception as e:
            result["ai_error"] = str(e)
    return result


# --- AI extraction via Gemini -------------------------------------------------
def call_gemini(key, model, prompt, json_mode=True, temperature=0.2):
    gen_cfg = {"temperature": temperature}
    if json_mode:
        gen_cfg["response_mime_type"] = "application/json"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    req = urllib.request.Request(
        GEMINI_ENDPOINT.format(model=model, key=key),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", {}).get("message", detail)
        except Exception:
            pass
        raise RuntimeError(f"Gemini API error {e.code}: {detail}")
    cands = data.get("candidates", [])
    if not cands:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(data)[:400]}")
    parts = cands[0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts).strip()


def do_ai(payload):
    key = (payload.get("gemini_key") or "").strip()
    if not key:
        raise ValueError("Add your Gemini API key in the API Keys tab first.")
    model = payload.get("model") or "gemini-2.5-flash"
    instruction = (payload.get("instruction") or "").strip()
    if not instruction:
        raise ValueError("Describe what you want to extract.")

    doc_id = payload.get("doc_id")
    if doc_id and doc_id in DOCS:
        content = page_to_markdown(DOCS[doc_id])
    else:
        content = payload.get("content", "")
    content = (content or "")[:120000]

    prompt = (
        "You are a precise web-scraping extraction assistant. "
        "Given the markdown content of a web page and an instruction, extract the "
        "requested information.\n\n"
        f"INSTRUCTION:\n{instruction}\n\n"
        "Respond ONLY with valid JSON (an object or array). Do not include markdown "
        "fences or commentary. If something is missing, use null.\n\n"
        f"PAGE CONTENT:\n{content}"
    )
    text = call_gemini(key, model, prompt, json_mode=True)
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        pass
    return {"model": model, "raw": text, "json": parsed, "chars": len(content)}


# --- capabilities probe -------------------------------------------------------
def capabilities():
    caps = {"basic": True, "stealthy": False, "dynamic": False, "gemini": True, "browsers": False}
    try:
        import scrapling.fetchers.stealth_chrome  # noqa: F401
        caps["stealthy"] = True
        import scrapling.fetchers.chrome  # noqa: F401
        caps["dynamic"] = True
    except Exception:
        pass
    for base in ("ms-playwright", "patchright"):
        p = os.path.expanduser(f"~/.cache/{base}")
        if os.path.isdir(p) and os.listdir(p):
            caps["browsers"] = True
    return caps


# --- HTTP plumbing ------------------------------------------------------------
ROUTES = {
    "/api/fetch": do_fetch,
    "/api/parse": do_parse,
    "/api/assets": do_assets,
    "/api/brand": do_brand,
    "/api/ai": do_ai,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "ScraplingDash/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[dash] " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                return self._send(404, b"index.html not found", "text/plain")
        if path == "/api/capabilities":
            return self._send(200, capabilities())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        handler = ROUTES.get(path)
        if not handler:
            return self._send(404, {"error": "unknown endpoint"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad request body: {e}"})
        try:
            return self._send(200, {"ok": True, "data": handler(payload)})
        except Exception as e:
            traceback.print_exc()
            return self._send(200, {"ok": False, "error": str(e), "type": type(e).__name__})


def main():
    caps = capabilities()
    print("=" * 64)
    print(" Scrapling Dashboard")
    print(f"   url        : http://{HOST}:{PORT}")
    print(f"   fetchers   : basic=on  stealthy={'on' if caps['stealthy'] else 'off'}"
          f"  dynamic={'on' if caps['dynamic'] else 'off'}")
    browsers = "installed" if caps["browsers"] else "NOT installed (python3.10 -m playwright install chromium)"
    print(f"   browsers   : {browsers}")
    print("   ai         : Gemini (REST) — add key in the UI")
    print("=" * 64)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
