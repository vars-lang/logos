# muse-browse

A small local HTTP API so Muse can do research through **your** browser. Read-only:
search, open a page, get text, screenshot. Checkout/payment URLs are refused.
Listens on `127.0.0.1` only, so Muse reaches it over SSH, and every call needs a token.

## Setup (Python 3.11)

```bash
pip install playwright        # no browser download needed, we use your Chrome

# 1. Start Chrome with a dedicated profile (Chrome won't allow debugging on your default one)
#    macOS:
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 --user-data-dir="$HOME/.muse-chrome"
#    Windows: chrome.exe --remote-debugging-port=9222 --user-data-dir=%USERPROFILE%\.muse-chrome
#    Linux:   google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.muse-chrome

# 2. In that Chrome window, sign in to whatever sites you want Muse to see (once).

# 3. Start the API
MUSE_BROWSE_TOKEN=pick-a-long-random-string python3 server.py
```

## Calling it (from Muse over SSH, or yourself)

```bash
H='Authorization: Bearer pick-a-long-random-string'
curl -s -H "$H" -d '{"query":"best 65W USB-C chargers 2026"}'  http://127.0.0.1:8793/search
curl -s -H "$H" -d '{"url":"https://example.com","links":true}' http://127.0.0.1:8793/fetch
curl -s -H "$H" -d '{"url":"https://example.com"}'              http://127.0.0.1:8793/screenshot
```

## Security notes

- The dedicated profile means Muse only sees sites you signed in to *in that window*, never your everyday browser.
- Chrome's debug port (9222) gives full control of that profile. Keep it on localhost, don't forward it.
- Anyone with the token can browse as you. Treat it like a password.
