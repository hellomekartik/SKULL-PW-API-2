import os
import time
import random
import logging
import threading
import concurrent.futures
import asyncio
from contextlib import asynccontextmanager

import requests as std_requests
from curl_cffi import requests as cf_requests
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# --- Logging ------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --- Constants ----------------------------------------------------------------
STUDY_URL  = "https://pwthor.live/study"
API_BASE   = "https://pwthor.live/api"
COOKIE_TTL = 55 * 60  # 55 minutes in seconds

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=ipport&format=text&protocol=http&timeout=5000&country=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
]

STUDY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="124", "Chromium";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://pwthor.live/study",
    "Origin": "https://pwthor.live",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

# --- Session cache (cookie + working proxy, both cached together) -------------
_session = {"cookie": None, "proxy": None, "expiry": 0}
_lock = threading.Lock()

# --- Proxy list loader --------------------------------------------------------
def load_proxies():
    seen = set()
    result = []
    for src in PROXY_SOURCES:
        try:
            r = std_requests.get(src, timeout=10)
            for line in r.text.splitlines():
                p = line.strip()
                # Normalize: strip http:// prefix if present
                if p.startswith("http://") or p.startswith("https://"):
                    p = p.split("://", 1)[1]
                if p and ":" in p and p not in seen and not p.startswith("#"):
                    seen.add(p)
                    result.append(p)
        except Exception as e:
            log.warning("Proxy source failed: %s", src[:60])
    random.shuffle(result)
    log.info("Loaded %d unique proxies", len(result))
    return result[:250]

# --- Single proxy attempt -----------------------------------------------------
def try_proxy(proxy_addr):
    proxy_url = "http://" + proxy_addr
    try:
        resp = cf_requests.get(
            STUDY_URL,
            headers=STUDY_HEADERS,
            proxies={"http": proxy_url, "https": proxy_url},
            impersonate="chrome124",
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        # Extract auth_token from cookies
        cookie_val = resp.cookies.get("auth_token")
        if not cookie_val:
            raw = resp.headers.get("set-cookie", "")
            for part in raw.split(";"):
                p = part.strip()
                if p.startswith("auth_token="):
                    cookie_val = p.split("=", 1)[1]
                    break
        if cookie_val:
            return (proxy_addr, "auth_token=" + cookie_val)
    except Exception:
        pass
    return None

# --- Direct fetch attempt (no proxy -- works if server IP not blocked) ---------
def try_direct():
    try:
        resp = cf_requests.get(
            STUDY_URL,
            headers=STUDY_HEADERS,
            impersonate="chrome124",
            timeout=10,
        )
        if resp.status_code == 200:
            cookie_val = resp.cookies.get("auth_token")
            if not cookie_val:
                raw = resp.headers.get("set-cookie", "")
                for part in raw.split(";"):
                    p = part.strip()
                    if p.startswith("auth_token="):
                        cookie_val = p.split("=", 1)[1]
                        break
            if cookie_val:
                return (None, "auth_token=" + cookie_val)
    except Exception:
        pass
    return None

# --- Full session refresh -----------------------------------------------------
def refresh_session():
    log.info("Refreshing session...")

    # 1. Try direct (Koyeb IP might work for some CF configs)
    result = try_direct()
    if result:
        _, cookie = result
        with _lock:
            _session["cookie"] = cookie
            _session["proxy"]  = None
            _session["expiry"] = time.time() + COOKIE_TTL
        log.info("Session via direct fetch OK")
        return True

    # 2. Race all proxies in parallel
    proxies = load_proxies()
    found = None

    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        futures = {ex.submit(try_proxy, p): p for p in proxies}
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            if res:
                found = res
                for f in futures:
                    f.cancel()
                break

    if found:
        proxy_addr, cookie = found
        with _lock:
            _session["cookie"] = cookie
            _session["proxy"]  = "http://" + proxy_addr
            _session["expiry"] = time.time() + COOKIE_TTL
        log.info("Session via proxy %s OK", proxy_addr)
        return True

    log.error("Session refresh failed -- all proxies exhausted")
    return False

# --- Get current valid session (from cache or refresh) -----------------------
def get_session():
    with _lock:
        if _session["cookie"] and time.time() < _session["expiry"]:
            return _session["cookie"], _session["proxy"]
    ok = refresh_session()
    if ok:
        with _lock:
            return _session["cookie"], _session["proxy"]
    return None, None

# --- Invalidate cache (force refresh on next call) ---------------------------
def invalidate():
    with _lock:
        _session["expiry"] = 0

# --- FastAPI lifespan: pre-warm session on startup ----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, refresh_session)
    yield

# --- App setup ----------------------------------------------------------------
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# --- Root: health check -------------------------------------------------------
@app.get("/")
async def root():
    with _lock:
        cookie  = _session["cookie"]
        proxy   = _session["proxy"]
        expiry  = _session["expiry"]
    return {
        "status":          "running",
        "session":         "active" if cookie and time.time() < expiry else "expired",
        "proxy":           proxy or "direct",
        "expires_in_min":  max(0, int((expiry - time.time()) / 60)),
        "usage":           "GET /<apiRoute>?<params>  ->  pwthor.live/api/<apiRoute>?<params>",
    }

# --- Proxy any route to pwthor.live/api --------------------------------------
@app.api_route("/{full_path:path}", methods=["GET", "POST"])
async def proxy(full_path: str, request: Request):
    if not full_path:
        return await root()

    cookie, proxy = get_session()

    if not cookie:
        return JSONResponse(
            {"success": False, "error": "Session failed -- all proxies exhausted"},
            status_code=503,
        )

    qs     = str(request.url.query)
    target = API_BASE + "/" + full_path + ("?" + qs if qs else "")
    hdrs   = {**API_HEADERS, "Cookie": cookie}

    kwargs = {"headers": hdrs, "impersonate": "chrome124", "timeout": 20}
    if proxy:
        kwargs["proxies"] = {"http": proxy, "https": proxy}

    try:
        resp = cf_requests.get(target, **kwargs)

        # If WAF blocked -> invalidate session and retry once with fresh session
        if resp.status_code == 403 and (
            b"cloudflare" in resp.content.lower() or
            b"blocked" in resp.content.lower()
        ):
            log.warning("API call blocked (403). Refreshing session and retrying...")
            invalidate()
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, refresh_session)
            cookie, proxy = get_session()
            if cookie:
                hdrs["Cookie"] = cookie
                kwargs["headers"] = hdrs
                if proxy:
                    kwargs["proxies"] = {"http": proxy, "https": proxy}
                elif "proxies" in kwargs:
                    del kwargs["proxies"]
                resp = cf_requests.get(target, **kwargs)

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    except Exception as e:
        log.error("Request error: %s", e)
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)
