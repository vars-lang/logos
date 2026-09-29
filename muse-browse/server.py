#!/usr/bin/env python3
"""muse-browse: a small local HTTP API that lets Muse use YOUR browser for research.

Muse SSHes into this machine and calls http://127.0.0.1:8793. The API drives a
Chrome you started yourself (with a dedicated profile), so pages load as a normal
signed-in browser rather than a scripted client. It is read-only: it searches,
opens pages and returns text. It refuses checkout/payment URLs.

Endpoints (all need header  Authorization: Bearer <token>  except /health):
  GET  /health
  POST /search      {"query": "...", "max_results": 8}
  POST /fetch       {"url": "https://...", "max_chars": 20000, "links": false}
  POST /screenshot  {"url": "https://..."}   -> {"png_base64": "..."}
  POST /amazon      {"query": "...", "max_results": 10, "domain": "amazon.com"}
                    -> product titles, prices, ratings and clean /dp/ links
"""
import base64, json, os, re, secrets, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote_plus, urlparse

from playwright.sync_api import sync_playwright

HOST = "127.0.0.1"  # never bind anything else; reach it through SSH
PORT = int(os.environ.get("MUSE_BROWSE_PORT", "8793"))
CDP_URL = os.environ.get("MUSE_BROWSE_CDP", "http://127.0.0.1:9222")
TOKEN = os.environ.get("MUSE_BROWSE_TOKEN") or secrets.token_urlsafe(24)
NAV_TIMEOUT_MS = 20_000
DEFAULT_CHARS = 20_000

# Research only: never navigate to anything that looks like buying or paying.
BLOCKED = re.compile(r"/(checkout|gp/buy|buy/|payment|pay/|order-confirmation|cart/checkout)", re.I)


class BadRequest(Exception):
    pass


def check_url(url):
    u = urlparse(url or "")
    if u.scheme not in ("http", "https") or not u.netloc:
        raise BadRequest("url must be http(s)")
    if BLOCKED.search(u.path):
        raise BadRequest("checkout/payment URLs are blocked (read-only research API)")
    return url


class Browser:
    def __init__(self):
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            sys.exit(f"Can't reach Chrome at {CDP_URL}: {e}\nStart Chrome first (see README).")
        self.ctx = self.browser.contexts[0]

    def open(self, url):
        check_url(url)
        page = self.ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(800)  # let late JS settle
            check_url(page.url)  # a redirect must not land on checkout either
        except BadRequest:
            page.close()
            raise
        except Exception:
            pass  # partial loads are still useful; return what rendered
        return page

    def fetch(self, url, max_chars=DEFAULT_CHARS, links=False):
        page = self.open(url)
        try:
            out = {
                "url": page.url,
                "title": page.title(),
                "text": page.inner_text("body")[:max_chars],
            }
            if links:
                out["links"] = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.slice(0,200).map(a => ({text: a.innerText.trim().slice(0,120), href: a.href}))",
                )
            return out
        finally:
            page.close()

    def search(self, query, max_results=8):
        page = self.open("https://html.duckduckgo.com/html/?q=" + quote_plus(query))
        try:
            results = page.eval_on_selector_all(
                ".result",
                """els => els.map(e => {
                    const a = e.querySelector('a.result__a');
                    const s = e.querySelector('.result__snippet');
                    return a ? {title: a.innerText.trim(), url: a.href,
                                snippet: s ? s.innerText.trim() : ''} : null;
                }).filter(Boolean)""",
            )
            return {"query": query, "results": results[:max_results]}
        finally:
            page.close()

    def amazon(self, query, max_results=10, domain="amazon.com"):
        if not re.fullmatch(r"(www\.)?amazon\.[a-z.]{2,6}", domain):
            raise BadRequest("domain must be an amazon domain")
        page = self.open(f"https://www.{domain.removeprefix('www.')}/s?k=" + quote_plus(query))
        try:
            if page.query_selector("form[action*='validateCaptcha']"):
                raise BadRequest("Amazon is showing a captcha: solve it once in the Chrome window, then retry")
            items = page.eval_on_selector_all(
                "div[data-component-type='s-search-result'][data-asin]",
                """els => els.filter(e => e.dataset.asin).map(e => {
                    const t = e.querySelector('h2');
                    const p = e.querySelector('.a-price .a-offscreen');
                    const r = e.querySelector('.a-icon-alt');
                    return {asin: e.dataset.asin,
                            title: t ? t.innerText.trim() : '',
                            price: p ? p.textContent.trim() : null,
                            rating: r ? r.textContent.trim() : null,
                            sponsored: /sponsored/i.test(e.innerText.slice(0, 200))};
                })""",
            )
            base = f"https://www.{domain.removeprefix('www.')}"
            for i in items:
                i["link"] = f"{base}/dp/{i['asin']}"
            return {"query": query, "results": items[:max_results]}
        finally:
            page.close()

    def screenshot(self, url):
        page = self.open(url)
        try:
            return {"url": page.url, "png_base64": base64.b64encode(page.screenshot()).decode()}
        finally:
            page.close()


BROWSER = None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        got = self.headers.get("Authorization", "")
        return secrets.compare_digest(got, f"Bearer {TOKEN}")

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "bad or missing token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/search":
                q = (body.get("query") or "").strip()
                if not q:
                    raise BadRequest("query required")
                res = BROWSER.search(q, int(body.get("max_results", 8)))
            elif self.path == "/fetch":
                res = BROWSER.fetch(body.get("url"), int(body.get("max_chars", DEFAULT_CHARS)), bool(body.get("links")))
            elif self.path == "/amazon":
                q = (body.get("query") or "").strip()
                if not q:
                    raise BadRequest("query required")
                res = BROWSER.amazon(q, int(body.get("max_results", 10)), body.get("domain", "amazon.com"))
            elif self.path == "/screenshot":
                res = BROWSER.screenshot(body.get("url"))
            else:
                return self._send(404, {"error": "not found"})
            self._send(200, res)
        except BadRequest as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    BROWSER = Browser()
    print(f"muse-browse listening on http://{HOST}:{PORT}")
    print(f"token: {TOKEN}")
    # Single-threaded on purpose: Playwright's sync API isn't thread-safe.
    HTTPServer((HOST, PORT), Handler).serve_forever()
