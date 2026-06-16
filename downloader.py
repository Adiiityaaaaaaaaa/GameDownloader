"""
A JDownloader-style GUI download manager.

Features
--------
- Paste a list of *direct* download URLs (one per line) or load them from a .txt file.
- Parallel downloads with a configurable worker count.
- Resume support (HTTP Range) for interrupted downloads.
- Live per-file progress, speed and status in a table.
- Automatic detection + extraction of multi-part archives
  (.partNN.rar, .7z.001, .zip, .rNN) once every part is present, using 7-Zip.
- Host link resolution: paste a host *page* link (e.g. a fuckingfast.co page)
  and the resolver fetches the page and pulls out the real direct file link
  before downloading. Plain direct URLs still work unchanged.

You are responsible for only downloading files you have the right to download.
"""

import os
import re
import sys
import json
import time
import html
import queue
import hashlib
import urllib.parse
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, unquote

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK = 1024 * 256  # 256 KiB

# Auto-retry policy for transient network/server failures. The Nth retry waits
# RETRY_BACKOFF[N-1] seconds (the last value repeats if attempts exceed it).
MAX_ATTEMPTS = 4
RETRY_BACKOFF = (3, 8, 20)

# Suffix for in-progress files. We download to "<name>.part" and only rename to
# the final name on success, so a half-finished file is never mistaken for a
# complete archive by the auto-extractor or by anything else scanning the dir.
PART_SUFFIX = ".part"

# Run child processes (7-Zip) with no console window flashing on screen.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class TransientError(Exception):
    """A failure worth retrying (timeout, dropped connection, 5xx, 429)."""


def _is_retryable(exc):
    """Classify an exception as a transient (retry) vs permanent failure."""
    if isinstance(exc, TransientError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (408, 425, 429, 500, 502, 503, 504)
    # URLError, socket.timeout, ConnectionReset, and most other I/O are OSError.
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


class RateLimiter:
    """
    A thread-safe token-bucket bandwidth throttle shared by all download
    threads. rate_bps is bytes/second; 0 means unlimited.
    """

    def __init__(self, rate_bps=0):
        self._lock = threading.Lock()
        self._rate = max(0, rate_bps)
        self._allowance = float(self._rate)
        self._last = time.monotonic()

    def set_rate(self, rate_bps):
        with self._lock:
            self._rate = max(0, rate_bps)
            self._allowance = float(self._rate)
            self._last = time.monotonic()

    def throttle(self, nbytes):
        """Block as needed so the sustained transfer rate stays under the cap."""
        if self._rate <= 0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                self._allowance += (now - self._last) * self._rate
                self._last = now
                cap = max(self._rate, nbytes)  # allow a 1s burst, never deadlock
                if self._allowance > cap:
                    self._allowance = cap
                if self._allowance >= nbytes:
                    self._allowance -= nbytes
                    return
                wait = (nbytes - self._allowance) / self._rate
            time.sleep(min(wait, 0.5))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _bundled_7zip():
    """
    A 7-Zip shipped alongside the program, so end users need not install it.

    Looked for (in order) inside a PyInstaller bundle, next to the .exe, and
    next to this script -- each optionally in a '7-zip' subfolder. RAR support
    needs BOTH 7z.exe and 7z.dll sitting together, which the build provides.
    """
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(meipass)                     # PyInstaller unpack dir
        roots.append(os.path.dirname(sys.executable))  # next to the .exe
    roots.append(os.path.dirname(os.path.abspath(__file__)))  # dev: next to .py
    for root in roots:
        for sub in ("", "7-zip", "7zip"):
            cand = os.path.join(root, sub, "7z.exe")
            if os.path.isfile(cand):
                return cand
    return None


def find_7zip():
    """Locate a 7-Zip executable, preferring a bundled copy, or return None."""
    bundled = _bundled_7zip()
    if bundled:
        return bundled
    for cand in (
        shutil.which("7z"),
        shutil.which("7za"),
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if cand and os.path.isfile(cand):
            return cand
    return None


SEVEN_ZIP = find_7zip()


def filename_from_url(url):
    """Derive a filename from a URL. Honour a #name fragment first."""
    parsed = urlparse(url)
    if parsed.fragment:
        frag = unquote(parsed.fragment)
        # keep only the final path-ish token, strip junk
        name = frag.split("/")[-1].strip()
        if name:
            return sanitize(name)
    name = unquote(os.path.basename(parsed.path)) or "download.bin"
    return sanitize(name)


def sanitize(name):
    """Make a string safe to use as a Windows filename."""
    name = name.replace("\\", "_").replace("/", "_")
    name = re.sub(r'[<>:"|?*]', "_", name)
    return name.strip(" .") or "download.bin"


def human(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# --------------------------------------------------------------------------- #
# Host link resolution
#
# Some hosts hand you a *page* URL (with an ad / "are you a bot" interstitial)
# instead of a direct file link. A resolver fetches that page and digs the real
# direct download link out of it. Unknown hosts fall through unchanged, so plain
# direct URLs keep working exactly as before.
# --------------------------------------------------------------------------- #
# curl_cffi impersonates a real Chrome's TLS fingerprint, which is what gets
# past Cloudflare's anti-bot check. Optional: if it isn't installed we fall back
# to urllib (which Cloudflare-protected hosts will usually 403).
try:
    from curl_cffi import requests as _cffi_requests
except Exception:  # noqa
    _cffi_requests = None

# Optional last-resort fallback: a real headless browser that can solve a full
# Cloudflare JS challenge. Only used if installed (`pip install playwright` then
# `playwright install chromium`); otherwise we just report the challenge.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
except Exception:  # noqa
    _sync_playwright = None


_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-browser-verification",
    "enable javascript and cookies",
    "_cf_chl_opt",
)


def _looks_like_challenge(html):
    """True if the body is a Cloudflare interstitial rather than the real page."""
    low = html.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def _fetch_with_browser(url, referer=None, timeout=45):
    """Load a page in headless Chromium, wait out Cloudflare + JS, return HTML."""
    if _sync_playwright is None:
        raise RuntimeError("playwright not installed")
    with _sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                extra_http_headers={"Referer": referer} if referer else {},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa
                pass
            # Poll until any Cloudflare interstitial clears.
            for _ in range(timeout):
                body = page.content()
                if not _looks_like_challenge(body):
                    return body
                page.wait_for_timeout(1000)
            return page.content()
        finally:
            browser.close()


def _fetch_text(url, referer=None, timeout=30, force_browser=False):
    """GET a URL and return the decoded body as text, beating Cloudflare if we can.

    force_browser=True renders with headless Chromium up front (for pages whose
    content is injected by JavaScript or sit behind a hard Cloudflare gate).
    """
    if force_browser and _sync_playwright is not None:
        try:
            return _fetch_with_browser(url, referer, max(timeout, 45))
        except Exception:  # noqa - fall back to the fast path below
            pass

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        headers["Referer"] = referer

    if _cffi_requests is not None:
        # impersonate a recent Chrome so Cloudflare serves the real page
        try:
            r = _cffi_requests.get(
                url, headers=headers, impersonate="chrome", timeout=timeout
            )
        except Exception as e:  # noqa - curl_cffi network errors are transient
            raise TransientError(f"fetch error: {e.__class__.__name__}")
        if r.status_code == 429 or r.status_code >= 500:
            raise TransientError(f"HTTP {r.status_code} fetching page")
        if r.status_code >= 400:
            # A hard block (e.g. DODI 403) -> try a real browser before giving up.
            if _sync_playwright is not None:
                try:
                    return _fetch_with_browser(url, referer, max(timeout, 45))
                except Exception:  # noqa
                    pass
            raise RuntimeError(f"HTTP {r.status_code} fetching page")
        text = r.text
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")

    # If the fast path hit a JS challenge, escalate to a real browser if we can.
    if _looks_like_challenge(text) and _sync_playwright is not None:
        try:
            return _fetch_with_browser(url, referer, max(timeout, 45))
        except Exception:  # noqa - fall through; resolver reports the challenge
            pass
    return text


def _post_form(url, data, referer=None, timeout=30):
    """POST a urlencoded form and return the response body (for XFileSharing hosts)."""
    headers = {"User-Agent": USER_AGENT,
               "Content-Type": "application/x-www-form-urlencoded"}
    if referer:
        headers["Referer"] = referer
    if _cffi_requests is not None:
        r = _cffi_requests.post(url, data=data, headers=headers,
                                impersonate="chrome", timeout=timeout)
        return r.text
    import urllib.parse
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _resolve_fuckingfast(url):
    """
    Resolve a fuckingfast.co page link to its real direct file link.

    The page's download() handler calls window.open("<direct link>"). The URL
    may be JS-escaped (https:\\/\\/…), so we match loosely and undo escaping.
    """
    html = _fetch_text(url, referer="https://fitgirl-repacks.site/")

    # Primary: the link passed to window.open(...).
    m = re.search(r'window\.open\(\s*["\'](https?:[^"\']+)["\']', html)
    if not m:
        # Secondary: any fuckingfast /dl/ link in the page (escaped or not).
        m = re.search(r'https:(?:\\?/){2}[^"\'\s]*fuckingfast\.co(?:\\?/)+dl[^"\'\s]+', html)
    if not m:
        if _looks_like_challenge(html):
            raise RuntimeError(
                "blocked by Cloudflare challenge (curl_cffi couldn't pass it)"
            )
        _dump_debug_html(url, html)
        raise RuntimeError("direct link not found (page saved to _debug_fuckingfast.html)")

    link = (m.group(1) if m.lastindex else m.group(0))
    link = link.replace("\\/", "/").replace("\\", "")
    return link.rstrip("\\\"'")


def _dump_debug_html(url, html):
    """Save a fetched page we couldn't parse, so the pattern can be fixed."""
    try:
        with open(os.path.join(app_dir(), "_debug_fuckingfast.html"), "w",
                  encoding="utf-8") as f:
            f.write(f"<!-- source: {url} -->\n")
            f.write(html)
    except Exception:  # noqa
        pass


# Hosts that are *index* pages listing many file links rather than a single
# file. Pasting one of these expands into every fuckingfast.co link on it.
_INDEX_HOSTS = ("fitgirl-repacks.site",)


def is_index_page(url):
    """True if url is a release/index page we should scrape for file links."""
    host = urlparse(url).netloc.lower()
    return any(h in host for h in _INDEX_HOSTS)


def extract_links_from_page(page_url):
    """
    Fetch an index page (e.g. a fitgirl-repacks.site release) and return every
    fuckingfast.co file link on it, de-duplicated and in page order.
    """
    html = _fetch_text(page_url, referer="https://fitgirl-repacks.site/")
    found = re.findall(r'https?://fuckingfast\.co/[^\s"\'<>\\)]+', html)
    seen, out = set(), []
    for u in found:
        u = u.rstrip("\\\"').,")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    if not out:
        if _looks_like_challenge(html):
            raise RuntimeError("blocked by Cloudflare challenge")
        raise RuntimeError("no fuckingfast.co links found on page")
    return out


FITGIRL_BASE = "https://fitgirl-repacks.site"
# Posts that aren't actual games -- filtered out of the games list.
_FITGIRL_SKIP = ("upcoming repack", "updates digest", "how to ", "faq",
                 "all my repacks", "discord", "donation")


def _fitgirl_rest(page, query):
    """Fetch a listing via the WordPress REST API (clean JSON)."""
    api = f"{FITGIRL_BASE}/wp-json/wp/v2/posts?per_page=30&page={page}"
    if query:
        api += "&search=" + urllib.parse.quote(query)
    data = json.loads(_fetch_text(api, referer=FITGIRL_BASE + "/"))
    games = []
    for p in data:
        title = html.unescape(re.sub(r"<.*?>", "", p.get("title", {})
                                     .get("rendered", ""))).strip()
        if title and p.get("link"):
            games.append({"title": title, "url": p["link"]})
    return games


def _fitgirl_html(page, query):
    """Scrape a listing/search page (FitGirl's own search is very accurate)."""
    if query:
        q = urllib.parse.quote(query)
        base = f"{FITGIRL_BASE}/?s={q}" if page == 1 else f"{FITGIRL_BASE}/page/{page}/?s={q}"
    else:
        base = FITGIRL_BASE + "/" if page == 1 else f"{FITGIRL_BASE}/page/{page}/"
    page_html = _fetch_text(base, referer=FITGIRL_BASE + "/")
    games = []
    for m in re.finditer(
        r'<h\d[^>]*class="entry-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        page_html, re.S,
    ):
        title = html.unescape(re.sub(r"<.*?>", "", m.group(2))).strip()
        if title:
            games.append({"title": title, "url": m.group(1)})
    return games


def fetch_fitgirl_games(page=1, query=None):
    """
    Return a list of {'title','url'} FitGirl repacks for a listing page or a
    search. Search uses FitGirl's own HTML search (most accurate); the latest
    listing uses the REST API. Each path falls back to the other on failure.
    """
    games = []
    try:
        games = _fitgirl_html(page, query) if query else _fitgirl_rest(page, query)
    except Exception:  # noqa
        games = []
    if not games:
        try:
            games = _fitgirl_rest(page, query) if query else _fitgirl_html(page, query)
        except Exception:  # noqa
            games = []
    # drop obvious non-game posts
    return [g for g in games
            if not any(s in g["title"].lower() for s in _FITGIRL_SKIP)]


# --------------------------------------------------------------------------- #
# DODI repacks (same WordPress/entry-title layout as FitGirl)
# --------------------------------------------------------------------------- #
DODI_BASE = "https://dodi-repacks.site"


def fetch_dodi_games(page=1, query=None):
    if query:
        q = urllib.parse.quote(query)
        base = f"{DODI_BASE}/?s={q}" if page == 1 else f"{DODI_BASE}/page/{page}/?s={q}"
    else:
        base = DODI_BASE + "/" if page == 1 else f"{DODI_BASE}/page/{page}/"
    page_html = _fetch_text(base, referer=DODI_BASE + "/", force_browser=True)
    games = []
    for m in re.finditer(
        r'<h\d[^>]*class="entry-title"[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        page_html, re.S,
    ):
        title = html.unescape(re.sub(r"<.*?>", "", m.group(2))).strip()
        if title:
            games.append({"title": title, "url": m.group(1)})
    return [g for g in games
            if not any(s in g["title"].lower() for s in _FITGIRL_SKIP)]


# --------------------------------------------------------------------------- #
# SteamRIP -- the whole catalogue lives on one static /games-list/ page, so we
# fetch it once and filter locally for search.
# --------------------------------------------------------------------------- #
STEAMRIP_BASE = "https://steamrip.com"
_steamrip_cache = []


def _steamrip_all():
    global _steamrip_cache
    if _steamrip_cache:
        return _steamrip_cache
    h = _fetch_text(STEAMRIP_BASE + "/games-list/", referer=STEAMRIP_BASE + "/")
    games, seen = [], set()
    # Slugs may carry a version/suffix after "free-download" (e.g.
    # /alan-wake-2-free-download-v12/, /...-free-download-m1/), so match loosely.
    for m in re.finditer(
        r'<a[^>]+href="((?:https://steamrip\.com)?/[a-z0-9-]*free-download[a-z0-9-]*/?)"[^>]*>(.*?)</a>',
        h, re.S,
    ):
        url = m.group(1)
        if url.startswith("/"):
            url = STEAMRIP_BASE + url
        title = html.unescape(re.sub(r"<.*?>", "", m.group(2))).strip()
        if title and url not in seen:
            seen.add(url)
            games.append({"title": title, "url": url})
    _steamrip_cache = games
    return games


def fetch_steamrip_games(page=1, query=None):
    games = _steamrip_all()
    if query:
        ql = query.lower()
        games = [g for g in games if ql in g["title"].lower()]
    per = 40
    return games[(page - 1) * per: page * per]


# site key -> (display name, fetcher)
SITES = {
    "fitgirl": ("FitGirl Repacks", fetch_fitgirl_games),
    "dodi": ("DODI Repacks", fetch_dodi_games),
    "steamrip": ("SteamRIP", fetch_steamrip_games),
}


# Hosts we can resolve to a real direct download (auto-queued).
_AUTO_HOSTS = ("fuckingfast.co", "pixeldrain.com", "datanodes.to")
# Hosts that gate downloads (premium/JS/captcha) -- opened in the browser instead.
_MANUAL_HOSTS = ("gofile.io", "megadb.net", "buzzheavier", "1fichier.com",
                 "filecrypt", "akirabox", "mega.nz", "mediafire.com")
_ALL_HOSTS = _AUTO_HOSTS + _MANUAL_HOSTS


def extract_download_links(page_url):
    """
    Return (auto_links, manual_links) found on a repack/game page. auto_links
    are direct-download hosts we resolve ourselves; manual_links are gated hosts
    (gofile etc.) the user must finish in a browser. Handles protocol-relative
    (//host/...) links too.
    """
    force = "steamrip.com" not in page_url  # steamrip links sit in static HTML
    page = _fetch_text(page_url, referer=page_url, force_browser=force)
    auto, manual, seen = [], [], set()
    for m in re.finditer(r'(?:https?:)?//[^\s"\'<>\\)]+', page):
        raw = m.group(0)
        low = raw.lower()
        if not any(h in low for h in _ALL_HOSTS):
            continue
        url = ("https:" + raw) if raw.startswith("//") else raw
        url = url.rstrip("\\\"').,")
        if url in seen:
            continue
        seen.add(url)
        if any(h in low for h in _AUTO_HOSTS):
            auto.append(url)
        else:
            manual.append(url)
    return auto, manual


def _resolve_pixeldrain(url):
    """pixeldrain.com/u/<id> -> direct API file URL (deterministic, no fetch)."""
    m = (re.search(r"pixeldrain\.com/u/([A-Za-z0-9]+)", url)
         or re.search(r"pixeldrain\.com/api/file/([A-Za-z0-9]+)", url)
         or re.search(r"pixeldrain\.com/l/([A-Za-z0-9]+)", url))
    if not m:
        raise RuntimeError("could not parse a pixeldrain file id")
    return f"https://pixeldrain.com/api/file/{m.group(1)}?download"


def _resolve_datanodes(url):
    """
    datanodes.to download page -> direct link (XFileSharing two-step). Best
    effort: hosts like this change their markup, so it may need updating.
    """
    html = _fetch_text(url, referer=url)
    direct = re.search(r'(https?://[^\s"\']+datanodes[^\s"\']+/d/[^\s"\']+)', html)
    if direct:
        return direct.group(1)
    form = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"', html))
    if not form:
        raise RuntimeError("datanodes: no download form found (markup changed?)")
    form.setdefault("op", "download2")
    html2 = _post_form(url, form, referer=url)
    m = (re.search(r'(https?://[^\s"\']+/d/[^\s"\']+)', html2)
         or re.search(r'window\.location\.href\s*=\s*"([^"]+)"', html2)
         or re.search(r'href="(https?://[^"]+)"[^>]*>\s*(?:Download|Direct)', html2, re.I))
    if not m:
        raise RuntimeError("datanodes: direct link not found")
    return m.group(1)


# host substring -> resolver function
_RESOLVERS = {
    "fuckingfast.co": _resolve_fuckingfast,
    "pixeldrain.com": _resolve_pixeldrain,
    "datanodes.to": _resolve_datanodes,
}


# --------------------------------------------------------------------------- #
# MD5 checksum verification (FitGirl ships an MD5/ folder + a verify .bat)
# --------------------------------------------------------------------------- #
_MD5_LINE = re.compile(r"\b([0-9a-fA-F]{32})\b[ \t]+\*?(.+)")


def _md5_of_file(path, chunk=1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def _parse_md5_content(content, md5_filename):
    """Return [(target_basename, expected_hash), ...] from a checksum file body."""
    out = []
    for line in content.splitlines():
        m = _MD5_LINE.match(line.strip())
        if m:
            out.append((os.path.basename(m.group(2).strip()), m.group(1).lower()))
    if not out:
        bare = re.fullmatch(r"[0-9a-fA-F]{32}", content.strip() or "")
        if bare:
            target = md5_filename[:-4] if md5_filename.lower().endswith(".md5") else md5_filename
            out.append((target, content.strip().lower()))
    return out


def verify_md5_dir(extract_dir):
    """
    Verify files against checksums found in extract_dir and its MD5/ subfolder.
    Returns (ok_count, [mismatched names], [missing names]).
    """
    if not os.path.isdir(extract_dir):
        return 0, [], []
    sources, md5_root = [], None
    for name in os.listdir(extract_dir):
        p = os.path.join(extract_dir, name)
        if name.lower() == "md5" and os.path.isdir(p):
            md5_root = p
        elif os.path.isfile(p) and name.lower().endswith(".md5"):
            sources.append(p)
    if md5_root:
        sources += [os.path.join(md5_root, n) for n in os.listdir(md5_root)
                    if os.path.isfile(os.path.join(md5_root, n))]
    checks = {}
    for src in sources:
        try:
            with open(src, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError:
            continue
        for base, h in _parse_md5_content(content, os.path.basename(src)):
            checks[base] = h
    ok, bad, missing = 0, [], []
    for base, expected in checks.items():
        target = os.path.join(extract_dir, base)
        if not os.path.isfile(target):
            missing.append(base)
        elif _md5_of_file(target).lower() == expected:
            ok += 1
        else:
            bad.append(base)
    return ok, bad, missing


def resolve_to_direct(url):
    """
    Turn a host page URL into a real direct download URL.

    Returns the input unchanged when the host isn't recognised (i.e. it's
    assumed to already be a direct link). Raises on a recognised host whose
    page we failed to parse.
    """
    host = urlparse(url).netloc.lower()
    for needle, resolver in _RESOLVERS.items():
        if needle in host:
            return resolver(url)
    return url


def _has_ext(name):
    """True if name ends in something that looks like a real file extension."""
    return bool(re.search(r"\.[A-Za-z0-9]{1,5}$", name or ""))


def _filename_from_content_disposition(value):
    """Pull a filename out of a Content-Disposition header value, or None."""
    if not value:
        return None
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", value, re.I)
    if not m:
        m = re.search(r'filename="?([^";]+)"?', value, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    return None


# Patterns identifying one part of a multi-part archive.
_PART_PATTERNS = [
    re.compile(r"^(?P<base>.+?)\.part\d+\.rar$", re.I),     # foo.part01.rar
    re.compile(r"^(?P<base>.+?)\.7z\.\d+$", re.I),          # foo.7z.001
    re.compile(r"^(?P<base>.+?)\.zip\.\d+$", re.I),         # foo.zip.001
    re.compile(r"^(?P<base>.+?)\.z\d+$", re.I),             # foo.z01 (split zip)
    re.compile(r"^(?P<base>.+?)\.r\d+$", re.I),             # foo.r00 (old rar)
]


def archive_group(filename):
    """
    Return a stable group key if this file is part of a multi-part archive,
    else None. All parts of one archive share the same key.
    """
    for pat in _PART_PATTERNS:
        m = pat.match(filename)
        if m:
            return m.group("base").lower()
    return None


def archive_group_info(filename):
    """
    Like archive_group, but also returns a human label for the group.

    Returns (key, label) for a multi-part file, or (None, None) for a file that
    stands on its own. The key is lower-cased for stable matching; the label
    keeps the original casing of the archive's base name.
    """
    for pat in _PART_PATTERNS:
        m = pat.match(filename)
        if m:
            base = m.group("base")
            return base.lower(), base.rstrip(" .-_")
    return None, None


def is_extraction_entrypoint(filename):
    """The single file you hand to 7-Zip to extract the whole set."""
    f = filename.lower()
    return (
        f.endswith(".part01.rar")
        or f.endswith(".part1.rar")
        or f.endswith(".7z.001")
        or f.endswith(".zip.001")
        or f.endswith(".rar")        # single-volume rar
        or f.endswith(".zip")
        or f.endswith(".7z")
    )


# --------------------------------------------------------------------------- #
# Download task model
# --------------------------------------------------------------------------- #
def app_dir():
    """A stable folder next to the program (works when run as .py or a frozen .exe)."""
    if getattr(sys, "frozen", False):
        # PyInstaller: __file__ lives in a temp dir that's deleted on exit, so
        # anchor persistent files next to the actual executable instead.
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# Where the queue + settings are persisted between runs (next to the program).
STATE_FILE = os.path.join(app_dir(), ".pyget_state.json")
LOG_FILE = os.path.join(app_dir(), "pyget.log")

# Statuses that mean "finished, don't touch" -- preserved verbatim on reload.
_TERMINAL_STATUSES = ("Done", "Extracted", "Extract failed")


def _segs_done(segs_path):
    """Bytes downloaded so far per a segmented '.part.segs' sidecar, or None."""
    try:
        with open(segs_path, encoding="utf-8") as f:
            plan = json.load(f)["segs"]
        return sum(c - s for s, c, e in plan)
    except Exception:  # noqa
        return None


def free_space(path):
    """Bytes free on the drive that holds `path` (walks up to an existing dir)."""
    probe = path
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return shutil.disk_usage(probe or ".").free
    except Exception:  # noqa
        return None


def notify(title, message):
    """Best-effort Windows desktop toast; never raises. Falls back to a beep."""
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:  # noqa
        pass
    if sys.platform != "win32":
        return
    safe_t = title.replace("'", "")
    safe_m = message.replace("'", "")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
        "ContentType=WindowsRuntime]>$null;"
        "$tpl=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$tx=$tpl.GetElementsByTagName('text');"
        f"$tx.Item(0).AppendChild($tpl.CreateTextNode('{safe_t}'))>$null;"
        f"$tx.Item(1).AppendChild($tpl.CreateTextNode('{safe_m}'))>$null;"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($tpl);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'PyGet').Show($n);"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:  # noqa
        pass


class Task:
    def __init__(self, url, dest_dir):
        self.url = url
        self.name = filename_from_url(url)
        self.dest_dir = dest_dir
        self.path = os.path.join(dest_dir, self.name)
        self.total = None
        self.done = 0
        self.speed = 0.0
        self.status = "Queued"
        self.cancelled = False
        self.paused = False   # per-task pause flag (independent of "stop all")
        self.tree_id = None   # set by GUI

    @property
    def part_path(self):
        """The temp file we download into before renaming to the final name."""
        return self.path + PART_SUFFIX

    # ----- persistence ----- #
    def to_dict(self):
        return {
            "url": self.url,
            "name": self.name,
            "dest_dir": self.dest_dir,
            "total": self.total,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d):
        t = cls(d["url"], d["dest_dir"])
        if d.get("name"):
            t.name = d["name"]
            t.path = os.path.join(t.dest_dir, t.name)
        t.total = d.get("total")
        status = d.get("status", "Queued")
        segs = t.part_path + ".segs"
        # Bytes on disk are the source of truth. A finished file exists under its
        # final name; an interrupted one lives in "<name>.part" (possibly with a
        # ".part.segs" sidecar describing a segmented/multi-connection download).
        if os.path.exists(t.path) and not os.path.exists(t.part_path):
            t.done = os.path.getsize(t.path)
            if status not in _TERMINAL_STATUSES:
                status = "Done"
        elif os.path.exists(t.part_path):
            t.done = _segs_done(segs)  # None if no/invalid sidecar
            if t.done is None:
                t.done = os.path.getsize(t.part_path)
            if status not in _TERMINAL_STATUSES:
                status = "Paused" if t.done > 0 else "Queued"
        else:
            t.done = 0
            if status not in _TERMINAL_STATUSES:
                status = "Queued"
        t.status = status
        return t


# --------------------------------------------------------------------------- #
# Core engine (runs off the UI thread)
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, events):
        # events: a queue the GUI drains to refresh itself
        self.events = events
        self.tasks = []            # shared reference to the App's task list
        self.pool = None
        self.workers = 3
        self.timeout = 30
        self.max_attempts = MAX_ATTEMPTS
        self.auto_extract = False
        self.delete_archives = False
        self.connections = 1
        self.dest_dir = None
        self.limiter = RateLimiter(0)
        self.shutdown = threading.Event()   # app is closing -> abort everything
        self.lock = threading.Lock()
        self.active = 0                     # tasks currently in flight
        self.running_ids = set()            # de-dupe re-submits of a live task
        self._watcher_on = False
        self._pool_size = None              # actual max_workers of the live pool

    def emit(self, kind, task=None, **extra):
        self.events.put({"kind": kind, "task": task, **extra})

    def configure(self, workers, auto_extract, dest_dir,
                  timeout=30, max_attempts=MAX_ATTEMPTS, speed_limit=0,
                  connections=1, delete_archives=False):
        self.workers = max(1, workers)
        self.auto_extract = auto_extract
        self.dest_dir = dest_dir
        self.timeout = max(5, timeout)
        self.max_attempts = max(1, max_attempts)
        self.limiter.set_rate(speed_limit)
        self.connections = max(1, min(16, connections))
        self.delete_archives = delete_archives

    def _ensure_pool(self):
        if self.pool is None:
            self.pool = ThreadPoolExecutor(max_workers=self.workers)
            self._pool_size = self.workers

    def _ensure_watcher(self):
        if not self._watcher_on:
            self._watcher_on = True
            threading.Thread(target=self._extract_watcher, daemon=True).start()

    def start(self, tasks, workers, auto_extract, dest_dir,
              timeout=30, max_attempts=MAX_ATTEMPTS, speed_limit=0,
              connections=1, delete_archives=False):
        self.configure(workers, auto_extract, dest_dir, timeout, max_attempts,
                       speed_limit, connections, delete_archives)
        # Resize the worker pool when the parallel count changed and nothing is
        # in flight (a ThreadPoolExecutor can't be resized in place).
        with self.lock:
            idle = self.active == 0
        if self.pool is not None and idle and self._pool_size != self.workers:
            self.pool.shutdown(wait=False)
            self.pool = None
        self._ensure_pool()
        self._ensure_watcher()  # always run; it gates on self.auto_extract itself
        for t in tasks:
            t.paused = False
            self._submit(t)

    def _submit(self, task):
        """Queue one task onto the pool, guarding against double-submits."""
        self._ensure_pool()
        with self.lock:
            if id(task) in self.running_ids:
                return
            self.running_ids.add(id(task))
            self.active += 1
        self.pool.submit(self._worker, task)

    def pause(self, task):
        task.paused = True

    def pause_all(self):
        for t in self.tasks:
            t.paused = True

    def set_speed_limit(self, speed_limit):
        self.limiter.set_rate(speed_limit)

    def close(self):
        self.shutdown.set()
        if self.pool is not None:
            self.pool.shutdown(wait=False)

    def _worker(self, task):
        try:
            self._run_one(task)
        finally:
            with self.lock:
                self.running_ids.discard(id(task))
                self.active -= 1
                idle = self.active == 0
            if idle:
                self.emit("all_done")

    def _interruptible_sleep(self, seconds, task):
        """Sleep, but wake immediately if the task is paused or the app closes."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.shutdown.is_set() or task.paused:
                return
            time.sleep(0.2)

    # ----- single download with resume + auto-retry ----- #
    def _run_one(self, task):
        if self.shutdown.is_set():
            return
        if task.paused:
            task.status = "Paused"
            task.speed = 0
            self.emit("update", task)
            return
        for attempt in range(1, self.max_attempts + 1):
            if self.shutdown.is_set() or task.paused:
                return
            try:
                self._attempt_download(task)
                return  # success, or paused mid-stream -- either way, done here
            except Exception as e:  # noqa
                if self.shutdown.is_set() or task.paused:
                    return
                permanent = not _is_retryable(e)
                if permanent or attempt >= self.max_attempts:
                    label = "Failed" if not permanent else "Error"
                    task.status = f"{label}: {e}" if str(e) else f"{label}: {e.__class__.__name__}"
                    self.emit("log", text=f"[{task.name}] giving up: {e}")
                    self.emit("update", task)
                    return
                delay = RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)]
                task.speed = 0
                task.status = f"Retry {attempt}/{self.max_attempts - 1} in {delay}s"
                self.emit("log", text=f"[{task.name}] {e} -- retrying in {delay}s")
                self.emit("update", task)
                self._interruptible_sleep(delay, task)

    def _attempt_download(self, task):
        """One download attempt. Raises on failure (the caller decides to retry)."""
        # Resolve a host page link into a real direct file link. Host pages hand
        # out time-limited links, so resolve on every attempt (incl. resumes).
        download_url = resolve_to_direct(task.url)
        if download_url != task.url:
            self.emit("log", text=f"[resolve] {task.name} -> direct link")

        # If a completed final file already exists, we're done.
        if os.path.exists(task.path) and not os.path.exists(task.part_path):
            task.total = task.total or os.path.getsize(task.path)
            task.done = os.path.getsize(task.path)
            task.status = "Done"
            self.emit("update", task)
            self.emit("completed", task)
            return

        # Multi-connection (segmented) path -- only when enabled and the server
        # supports ranged requests. Falls back to single-stream otherwise.
        if self.connections > 1:
            total = self._probe_size(download_url, task)
            if total:
                self._segmented_download(task, download_url, total)
                return

        existing = os.path.getsize(task.part_path) if os.path.exists(task.part_path) else 0
        headers = {"User-Agent": USER_AGENT, "Referer": task.url}
        if existing:
            headers["Range"] = f"bytes={existing}-"

        req = urllib.request.Request(download_url, headers=headers)
        task.status = "Connecting"
        self.emit("update", task)

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status == 206:  # partial -> resuming
                cr = resp.headers.get("Content-Range", "")
                m = re.search(r"/(\d+)$", cr)
                task.total = int(m.group(1)) if m else None
                mode = "ab"
                task.done = existing
            else:
                # Fresh download: if we don't already have a real filename
                # (e.g. the page link had no #name fragment), adopt the one the
                # server advertises so the file lands with a sane name.
                if not _has_ext(task.name):
                    server_name = _filename_from_content_disposition(
                        resp.headers.get("Content-Disposition", "")
                    )
                    if server_name:
                        task.name = sanitize(server_name)
                        task.path = os.path.join(task.dest_dir, task.name)
                length = resp.headers.get("Content-Length")
                task.total = int(length) if length else None
                mode = "wb"
                task.done = 0
                existing = 0

            # Already complete on disk (the .part holds the whole file).
            if task.total is not None and existing >= task.total and existing > 0:
                os.replace(task.part_path, task.path)
                task.done = task.total
                task.status = "Done"
                self.emit("update", task)
                self.emit("completed", task)
                return

            task.status = "Downloading"
            last = time.monotonic()
            last_bytes = task.done
            with open(task.part_path, mode) as f:
                while True:
                    if self.shutdown.is_set() or task.paused:
                        task.status = "Paused"
                        task.speed = 0
                        self.emit("update", task)
                        return
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    f.write(buf)
                    task.done += len(buf)
                    self.limiter.throttle(len(buf))   # global bandwidth cap
                    now = time.monotonic()
                    if now - last >= 0.4:
                        task.speed = (task.done - last_bytes) / (now - last)
                        last, last_bytes = now, task.done
                        self.emit("update", task)

        # A truncated download (server closed early) is incomplete -> retry.
        if task.total is not None and task.done < task.total:
            raise TransientError(
                f"incomplete ({human(task.done)} of {human(task.total)})"
            )

        os.replace(task.part_path, task.path)  # atomic publish under final name
        task.status = "Done"
        task.speed = 0
        self.emit("update", task)
        self.emit("completed", task)

    # ----- segmented (multi-connection) download ----- #
    def _probe_size(self, url, task):
        """Return total size if the server supports ranged GETs, else None."""
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": USER_AGENT, "Referer": task.url, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status != 206:
                    return None
                m = re.search(r"/(\d+)$", resp.headers.get("Content-Range", ""))
                if not m:
                    return None
                if not _has_ext(task.name):
                    sn = _filename_from_content_disposition(
                        resp.headers.get("Content-Disposition", ""))
                    if sn:
                        task.name = sanitize(sn)
                        task.path = os.path.join(task.dest_dir, task.name)
                return int(m.group(1))
        except Exception:  # noqa
            return None

    def _save_segs(self, path, total, plan):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"total": total, "segs": plan}, f)
        except OSError:
            pass

    def _segmented_download(self, task, url, total):
        """Download `total` bytes over N ranged connections into one .part file."""
        n = self.connections
        part, segs_file = task.part_path, task.part_path + ".segs"
        task.total = total

        # Resume an existing plan, or build a fresh one (each seg = [start,cur,end]).
        plan = None
        if os.path.exists(part) and os.path.exists(segs_file):
            try:
                with open(segs_file, encoding="utf-8") as f:
                    saved = json.load(f)
                if saved.get("total") == total:
                    plan = saved["segs"]
            except Exception:  # noqa
                plan = None
        if plan is None:
            with open(part, "wb") as f:
                if total:
                    f.truncate(total)
            seg = total // n
            plan = [[i * seg, i * seg, (total - 1 if i == n - 1 else (i + 1) * seg - 1)]
                    for i in range(n)]

        task.done = sum(c - s for s, c, e in plan)
        lock = threading.Lock()
        errors = []

        def worker(idx):
            s, c, e = plan[idx]
            if c > e:
                return
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT, "Referer": task.url,
                    "Range": f"bytes={c}-{e}"})
                with urllib.request.urlopen(req, timeout=self.timeout) as resp, \
                        open(part, "r+b") as f:
                    f.seek(c)
                    while True:
                        if self.shutdown.is_set() or task.paused:
                            return
                        buf = resp.read(CHUNK)
                        if not buf:
                            break
                        f.write(buf)
                        with lock:
                            plan[idx][1] += len(buf)
                            task.done += len(buf)
                        self.limiter.throttle(len(buf))
            except Exception as ex:  # noqa
                with lock:
                    errors.append(ex)

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(len(plan))]
        task.status = "Downloading"
        self.emit("update", task)
        for th in threads:
            th.start()
        last, last_done = time.monotonic(), task.done
        while any(th.is_alive() for th in threads):
            time.sleep(0.4)
            now = time.monotonic()
            with lock:
                task.speed = (task.done - last_done) / (now - last) if now > last else 0
                self._save_segs(segs_file, total, plan)
            last, last_done = now, task.done
            self.emit("update", task)
        for th in threads:
            th.join(timeout=5)

        if self.shutdown.is_set() or task.paused:
            task.status = "Paused"
            task.speed = 0
            self._save_segs(segs_file, total, plan)
            self.emit("update", task)
            return
        if errors:
            self._save_segs(segs_file, total, plan)
            raise errors[0]
        if not all(c > e for s, c, e in plan):
            self._save_segs(segs_file, total, plan)
            raise TransientError("segmented download incomplete")

        os.replace(part, task.path)
        try:
            os.remove(segs_file)
        except OSError:
            pass
        task.done = total
        task.speed = 0
        task.status = "Done"
        self.emit("update", task)
        self.emit("completed", task)

    # ----- auto-extraction ----- #
    def _extract_watcher(self):
        # Runs for the lifetime of the app, extracting each archive group as
        # soon as all its parts finish. Poll is cheap (a dict build every ~1.5s).
        extracted_groups = set()
        while not self.shutdown.is_set():
            time.sleep(1.5)
            if not self.auto_extract:
                continue
            dest_dir = self.dest_dir

            # Build groups -> set of part names, and whether all parts done.
            groups = {}
            singles = []
            for t in self.tasks:
                g = archive_group(t.name)
                if g:
                    groups.setdefault(g, []).append(t)
                elif is_extraction_entrypoint(t.name):
                    singles.append(t)

            # Multi-part groups: extract when every part is downloaded.
            for g, parts in groups.items():
                if g in extracted_groups:
                    continue
                if all(p.status == "Done" for p in parts):
                    entry = self._pick_entry(parts)
                    if entry:
                        extracted_groups.add(g)
                        self._extract(entry)
                        if entry.status == "Extracted" and self.delete_archives:
                            self._delete_archive_files(parts)

            # Single archives.
            for t in singles:
                if t.name in extracted_groups:
                    continue
                if t.status == "Done":
                    extracted_groups.add(t.name)
                    self._extract(t)
                    if t.status == "Extracted" and self.delete_archives:
                        self._delete_archive_files([t])

    def extract_now(self, tasks):
        """Manually (re)extract the archive group(s) the selected tasks belong to."""
        if not SEVEN_ZIP:
            self.emit("log", text="[extract] 7-Zip not found")
            return
        done = set()
        keys = {archive_group(t.name) for t in tasks if archive_group(t.name)}
        for k in keys:
            parts = [t for t in self.tasks if archive_group(t.name) == k]
            entry = self._pick_entry(parts)
            if entry:
                done.add(entry.name)
                self._extract(entry)
                if entry.status == "Extracted" and self.delete_archives:
                    self._delete_archive_files(parts)
        for t in tasks:
            if archive_group(t.name) is None and is_extraction_entrypoint(t.name) \
                    and t.name not in done:
                self._extract(t)
                if t.status == "Extracted" and self.delete_archives:
                    self._delete_archive_files([t])

    def _delete_archive_files(self, parts):
        """After a verified extraction, reclaim space by removing the archives."""
        removed = 0
        for p in parts:
            for path in (p.path, p.part_path, p.part_path + ".segs"):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        if removed:
            self.emit("log", text=f"[cleanup] removed {removed} archive file(s) "
                                   f"after extraction")

    def _pick_entry(self, parts):
        # The first volume is always the lexicographically smallest part name
        # for .partNN.rar / .7z.NNN / .zNN / .rNN schemes (NN is zero-padded
        # and ordered). 7-Zip must be pointed at that first volume.
        return sorted(parts, key=lambda p: p.name.lower())[0]

    def _run_7z(self, args, task, label):
        """
        Run a 7-Zip command in the background (no console window) and stream its
        percentage progress into task.status as "<label> NN%".

        Returns (returncode, last_output_lines). returncode is None if the run
        was interrupted by a pause / app close.
        """
        try:
            proc = subprocess.Popen(
                args + ["-bsp1"],   # -bsp1: emit progress to stdout even when piped
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:  # noqa
            return -1, f"{e.__class__.__name__}: {e}"

        tail, pending, last_pct = [], b"", -1
        try:
            while True:
                if self.shutdown.is_set() or task.paused:
                    proc.terminate()
                    return None, "interrupted"
                chunk = (proc.stdout.read1(4096)
                         if hasattr(proc.stdout, "read1")
                         else proc.stdout.read(4096))
                if not chunk:
                    break
                pending += chunk
                segs = re.split(rb"[\r\n]", pending)
                pending = segs.pop()
                for raw in segs:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    tail.append(line)
                    del tail[:-5]
                    m = re.search(r"(\d+)%", line)
                    if m:
                        pct = int(m.group(1))
                        if pct != last_pct:
                            last_pct = pct
                            task.status = f"{label} {pct}%"
                            self.emit("update", task)
        finally:
            proc.wait()
        return proc.returncode, " ".join(tail[-3:])

    def _extract(self, task):
        if not SEVEN_ZIP:
            self.emit("log", text=f"[extract] 7-Zip not found, skipping {task.name}")
            return
        if not os.path.exists(task.path):
            self.emit("log", text=f"[extract] file missing, skipping {task.name}")
            return
        dest_dir = task.dest_dir  # extract next to where the files actually are

        # 1) Integrity check first: a corrupt/incomplete archive should not be
        # silently "extracted". 7-Zip's test mode reads every volume's CRCs.
        task.status = "Verifying"
        self.emit("update", task)
        rc, tail = self._run_7z([SEVEN_ZIP, "t", task.path, "-y"], task, "Verifying")
        if rc is None:
            return  # interrupted by pause/close
        if rc != 0:
            task.status = "Verify failed"
            self.emit("log", text=f"[verify] FAILED {task.name}: {tail}")
            self.emit("update", task)
            return
        self.emit("log", text=f"[verify] OK: {task.name}")

        # 2) Extract -- progress streamed into the row, no external window.
        out = os.path.join(dest_dir, "_extracted")
        os.makedirs(out, exist_ok=True)
        task.status = "Extracting"
        self.emit("update", task)
        self.emit("log", text=f"[extract] {task.name} -> {out}")
        rc, tail = self._run_7z(
            [SEVEN_ZIP, "x", task.path, f"-o{out}", "-y", "-aoa"], task, "Extracting"
        )
        if rc is None:
            return
        if rc == 0:
            task.status = "Extracted"
            self.emit("log", text=f"[extract] OK: {task.name}")
        else:
            task.status = "Extract failed"
            self.emit("log", text=f"[extract] FAILED {task.name}: {tail}")
        self.emit("update", task)

    # ----- re-check a task against what is actually on disk ----- #
    def recheck(self, task):
        """
        Reconcile a task's status with the file on disk. Detects a deleted /
        empty file, a partial (resumable) file, a wrong-size file, and -- for
        single-volume archives with 7-Zip available -- corruption.
        """
        final, part = task.path, task.part_path

        # Nothing under the final name -> missing, or resumable .part leftover.
        if not os.path.exists(final):
            if os.path.exists(part):
                task.done = os.path.getsize(part)
                task.status = "Paused"
                self.emit("log", text=f"[recheck] {task.name}: partial on disk -> resumable")
            else:
                task.done = 0
                task.status = "Missing"
                self.emit("log", text=f"[recheck] {task.name}: file is gone")
            self.emit("update", task)
            return

        size = os.path.getsize(final)

        if size == 0:  # empty file -> treat as missing
            try:
                os.remove(final)
            except OSError:
                pass
            task.done = 0
            task.status = "Missing"
            self.emit("log", text=f"[recheck] {task.name}: empty file -> will re-download")
            self.emit("update", task)
            return

        if task.total and size < task.total:
            # Incomplete final file -> turn it back into a .part so it resumes.
            try:
                if os.path.exists(part):
                    os.remove(part)
                os.replace(final, part)
            except OSError:
                pass
            task.done = size
            task.status = "Paused"
            self.emit("log", text=f"[recheck] {task.name}: incomplete "
                                   f"({human(size)}/{human(task.total)}) -> will resume")
            self.emit("update", task)
            return

        if task.total and size > task.total:
            try:
                os.remove(final)
            except OSError:
                pass
            task.done = 0
            task.status = "Missing"
            self.emit("log", text=f"[recheck] {task.name}: bigger than expected "
                                   f"({human(size)}>{human(task.total)}) -> will re-download")
            self.emit("update", task)
            return

        # Size looks right (or unknown). Deep-verify single-volume archives.
        task.done = size
        task.total = task.total or size
        deep = (SEVEN_ZIP and is_extraction_entrypoint(task.name)
                and archive_group(task.name) is None)
        if deep:
            task.status = "Verifying"
            self.emit("update", task)
            rc, tail = self._run_7z([SEVEN_ZIP, "t", final, "-y"], task, "Verifying")
            if rc is None:
                return  # interrupted
            if rc == 0:
                task.status = "Done"
                self.emit("log", text=f"[recheck] {task.name}: OK ({human(size)})")
            else:
                task.status = "Corrupt"
                self.emit("log", text=f"[recheck] {task.name}: CORRUPT ({tail})")
        else:
            task.status = "Done"
            self.emit("log", text=f"[recheck] {task.name}: present ({human(size)})")
        self.emit("update", task)


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PyGet - Download Manager")
        self.geometry("900x620")
        self.minsize(760, 520)

        self.events = queue.Queue()
        self.engine = Engine(self.events)
        self.dest_dir = tk.StringVar(value=os.path.join(app_dir(), "downloads"))
        self.workers = tk.IntVar(value=3)
        self.auto_extract = tk.BooleanVar(value=True)
        self.timeout = tk.IntVar(value=30)
        self.max_attempts = tk.IntVar(value=MAX_ATTEMPTS)
        self.speed_limit = tk.IntVar(value=0)        # KB/s, 0 = unlimited
        self.connections = tk.IntVar(value=1)        # segments per file (1 = off)
        self.delete_after_extract = tk.BooleanVar(value=False)
        self.watch_clipboard = tk.BooleanVar(value=False)
        self.to_tray = tk.BooleanVar(value=False)    # close/minimize hides to tray
        self.status_text = tk.StringVar(value="Idle")
        self._clip_last = ""
        self._tray = None

        self._build_ui()
        self.engine.tasks = self.tasks               # engine shares the live list
        self._load_state()
        self.speed_limit.trace_add("write", self._on_speed_limit_change)
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._setup_dnd()
        self.after(150, self._drain_events)
        self.after(1000, self._poll_clipboard)

    def _on_speed_limit_change(self, *_):
        try:
            self.engine.set_speed_limit(max(0, self.speed_limit.get()) * 1024)
        except Exception:  # noqa - ignore partial entry while typing
            pass

    # ----- persistence ----- #
    def _save_state(self):
        data = {
            "dest_dir": self.dest_dir.get(),
            "workers": self.workers.get(),
            "auto_extract": self.auto_extract.get(),
            "timeout": self.timeout.get(),
            "max_attempts": self.max_attempts.get(),
            "speed_limit": self.speed_limit.get(),
            "connections": self.connections.get(),
            "delete_after_extract": self.delete_after_extract.get(),
            "watch_clipboard": self.watch_clipboard.get(),
            "to_tray": self.to_tray.get(),
            "geometry": self.geometry(),
            "tasks": [t.to_dict() for t in self.tasks],
        }
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file
        except Exception as e:  # noqa
            self._log(f"[state] save failed: {e}")

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa
            self._log(f"[state] load failed: {e}")
            return
        if data.get("dest_dir"):
            self.dest_dir.set(data["dest_dir"])
        if data.get("workers"):
            self.workers.set(int(data["workers"]))
        if "auto_extract" in data:
            self.auto_extract.set(bool(data["auto_extract"]))
        if data.get("timeout"):
            self.timeout.set(int(data["timeout"]))
        if data.get("max_attempts"):
            self.max_attempts.set(int(data["max_attempts"]))
        if "speed_limit" in data:
            self.speed_limit.set(int(data["speed_limit"]))
        if data.get("connections"):
            self.connections.set(int(data["connections"]))
        if "delete_after_extract" in data:
            self.delete_after_extract.set(bool(data["delete_after_extract"]))
        if "watch_clipboard" in data:
            self.watch_clipboard.set(bool(data["watch_clipboard"]))
        if "to_tray" in data:
            self.to_tray.set(bool(data["to_tray"]))
        if data.get("geometry"):
            try:
                self.geometry(data["geometry"])
            except Exception:  # noqa
                pass
        restored = 0
        for d in data.get("tasks", []):
            try:
                t = Task.from_dict(d)
            except Exception:  # noqa
                continue
            self.tasks.append(t)
            self._insert_task_row(t)
            restored += 1
        if restored:
            self._log(f"Restored {restored} task(s) from last session.")

    def _on_window_close(self):
        # If "minimize to tray" is on and a tray icon is available, hide instead.
        if self.to_tray.get() and self._hide_to_tray():
            return
        self._on_close()

    def _on_close(self):
        self._save_state()
        self.engine.close()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:  # noqa
                pass
        self.destroy()

    # ----- layout ----- #
    def _build_ui(self):
        pad = dict(padx=8, pady=4)

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(
            top,
            text="Paste URLs (one per line) - direct links, fuckingfast.co "
            "pages, or a fitgirl-repacks.site page (auto-grabs all its links):",
        ).pack(anchor="w")
        self.url_text = tk.Text(top, height=6, wrap="none")
        self.url_text.pack(fill="x")

        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        ttk.Button(btns, text="Load .txt", command=self._load_txt).pack(side="left")
        ttk.Button(btns, text="Add to queue", command=self._add_to_queue).pack(
            side="left", padx=4
        )
        ttk.Button(btns, text="Clear queue", command=self._clear_queue).pack(side="left")
        ttk.Button(btns, text="Browse Repacks",
                   command=self._open_fitgirl).pack(side="right")

        # settings row
        cfg = ttk.Frame(self)
        cfg.pack(fill="x", **pad)
        ttk.Label(cfg, text="Save to:").pack(side="left")
        ttk.Entry(cfg, textvariable=self.dest_dir, width=38).pack(side="left", padx=4)
        ttk.Button(cfg, text="Browse", command=self._browse).pack(side="left")
        ttk.Label(cfg, text="  Parallel:").pack(side="left")
        ttk.Spinbox(cfg, from_=1, to=10, width=4, textvariable=self.workers).pack(
            side="left"
        )
        ttk.Checkbutton(
            cfg, text="Auto-extract", variable=self.auto_extract
        ).pack(side="left", padx=6)
        ttk.Checkbutton(
            cfg, text="Watch clipboard", variable=self.watch_clipboard
        ).pack(side="left", padx=6)
        ttk.Button(cfg, text="Settings...", command=self._open_settings).pack(side="right")

        # table -- a tree: multi-part archives are collapsible parent rows whose
        # children are the individual parts. The #0 (tree) column holds names.
        cols = ("size", "progress", "speed", "eta", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings", height=12)
        self.tree.heading("#0", text="Name")
        self.tree.column("#0", width=290, anchor="w", stretch=True)
        for c, w in zip(cols, (90, 140, 90, 80, 130)):
            self.tree.heading(c, text=c.upper() if c == "eta" else c.capitalize())
            self.tree.column(c, width=w, anchor="w", stretch=False)
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)
        # right-click context menu on rows
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        self._build_context_menu()

        # action row
        act = ttk.Frame(self)
        act.pack(fill="x", **pad)
        self.start_btn = ttk.Button(act, text="Start", command=self._start)
        self.start_btn.pack(side="left")
        ttk.Button(act, text="Pause/Stop all", command=self._stop).pack(
            side="left", padx=4
        )
        ttk.Button(act, text="Move up", command=lambda: self._move(-1)).pack(
            side="left", padx=(12, 2)
        )
        ttk.Button(act, text="Move down", command=lambda: self._move(1)).pack(
            side="left"
        )
        sz = "7-Zip: found" if SEVEN_ZIP else "7-Zip: NOT found (extraction disabled)"
        ttk.Label(act, text=sz).pack(side="right")

        # overall status bar + progress bar
        status = ttk.Frame(self)
        status.pack(fill="x", padx=8)
        ttk.Label(status, textvariable=self.status_text, anchor="w").pack(
            side="left", expand=True, fill="x"
        )
        self.overall = ttk.Progressbar(status, length=200, mode="determinate", maximum=1000)
        self.overall.pack(side="right", padx=4)

        # log
        ttk.Label(self, text="Log:").pack(anchor="w", padx=8)
        self.log = tk.Text(self, height=6, state="disabled", wrap="word")
        self.log.pack(fill="x", padx=8, pady=(0, 8))

        self.tasks = []
        self.groups = {}  # group key -> parent tree node id

    def _build_context_menu(self):
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Resume / Start", command=lambda: self._menu_action("resume"))
        m.add_command(label="Pause", command=lambda: self._menu_action("pause"))
        m.add_command(label="Retry", command=lambda: self._menu_action("retry"))
        m.add_command(label="Re-check files", command=lambda: self._menu_action("recheck"))
        m.add_command(label="Extract now", command=lambda: self._menu_action("extract"))
        m.add_command(label="Verify BINs (MD5)", command=lambda: self._menu_action("verify_bins"))
        m.add_separator()
        m.add_command(label="Move up", command=lambda: self._move(-1))
        m.add_command(label="Move down", command=lambda: self._move(1))
        m.add_separator()
        m.add_command(label="Open file", command=lambda: self._menu_action("open_file"))
        m.add_command(label="Open folder", command=lambda: self._menu_action("open_folder"))
        m.add_command(label="Copy URL", command=lambda: self._menu_action("copy_url"))
        m.add_separator()
        m.add_command(label="Remove", command=lambda: self._menu_action("remove"))
        m.add_command(label="Remove + delete file",
                      command=lambda: self._menu_action("remove_delete"))
        self.menu = m

    # ----- per-task actions / context menu ----- #
    def _tasks_for_item(self, item):
        """Tasks represented by a tree row: a single task, or all of a group."""
        if item in self.groups.values():
            kids = set(self.tree.get_children(item))
            return [t for t in self.tasks if t.tree_id in kids]
        return [t for t in self.tasks if t.tree_id == item]

    def _on_tree_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _on_tree_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.groups.values():
            tasks = self._tasks_for_item(row)
            if tasks:
                self._open_path(tasks[0].dest_dir)

    def _menu_action(self, action):
        tasks = []
        for item in self.tree.selection():
            tasks += self._tasks_for_item(item)
        if not tasks:
            return
        if action == "resume":
            self._resume_tasks(tasks)
        elif action == "pause":
            for t in tasks:
                self.engine.pause(t)
            self._log(f"Paused {len(tasks)} task(s).")
        elif action == "retry":
            self._resume_tasks(tasks, reset=True)
        elif action == "recheck":
            self._recheck_tasks(tasks)
        elif action == "extract":
            self._extract_now(tasks)
        elif action == "verify_bins":
            self._verify_bins(tasks)
        elif action == "open_file":
            self._open_path(tasks[0].path)
        elif action == "open_folder":
            self._open_path(tasks[0].dest_dir)
        elif action == "copy_url":
            self.clipboard_clear()
            self.clipboard_append("\n".join(t.url for t in tasks))
            self._log(f"Copied {len(tasks)} URL(s) to clipboard.")
        elif action == "remove":
            self._remove_tasks(tasks, delete=False)
        elif action == "remove_delete":
            self._remove_tasks(tasks, delete=True)

    def _resume_tasks(self, tasks, reset=False):
        run = []
        for t in tasks:
            if t.status in ("Done", "Extracted", "Extracting", "Verifying"):
                continue
            t.paused = False
            if reset:
                t.status = "Queued"
            run.append(t)
            self._refresh_row(t)
        if not run:
            return
        self.start_btn.config(state="disabled")
        self._log(f"{'Retrying' if reset else 'Resuming'} {len(run)} task(s)...")
        self.engine.start(
            run, self.workers.get(), self.auto_extract.get(), self.dest_dir.get(),
            self.timeout.get(), self.max_attempts.get(), self.speed_limit.get() * 1024,
            self.connections.get(), self.delete_after_extract.get(),
        )
        self._save_state()

    def _recheck_tasks(self, tasks):
        """Re-inspect selected files on disk (in the background, off the UI)."""
        busy = ("Downloading", "Connecting", "Verifying", "Extracting", "Retry")
        targets = [t for t in tasks if not t.status.startswith(busy)]
        skipped = len(tasks) - len(targets)
        if skipped:
            self._log(f"Re-check: skipped {skipped} busy task(s) (pause them first).")
        if not targets:
            return
        self._log(f"Re-checking {len(targets)} file(s) on disk...")
        threading.Thread(
            target=self._recheck_worker, args=(targets,), daemon=True
        ).start()

    def _recheck_worker(self, targets):
        for t in targets:
            try:
                self.engine.recheck(t)
            except Exception as e:  # noqa
                self.events.put({"kind": "log", "task": None,
                                 "text": f"[recheck] {t.name}: error {e}"})
        self.events.put({"kind": "rechecked", "task": None, "n": len(targets)})

    def _remove_tasks(self, tasks, delete):
        if delete and not messagebox.askyesno(
            "Delete files", f"Remove {len(tasks)} task(s) AND delete their files?"
        ):
            return
        for t in tasks:
            self.engine.pause(t)  # stop it if it's running
        for t in tasks:
            if t.tree_id and self.tree.exists(t.tree_id):
                parent = self.tree.parent(t.tree_id)
                self.tree.delete(t.tree_id)
                if parent and not self.tree.get_children(parent):
                    self.tree.delete(parent)
                    for k, v in list(self.groups.items()):
                        if v == parent:
                            del self.groups[k]
            if t in self.tasks:
                self.tasks.remove(t)
            if delete:
                for p in (t.path, t.part_path):
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
        self._log(f"Removed {len(tasks)} task(s)" + (" + files." if delete else "."))
        self._save_state()
        self._refresh_status()

    def _open_path(self, path):
        target = path if os.path.exists(path) else (os.path.dirname(path) or ".")
        try:
            os.startfile(target)  # Windows
        except AttributeError:
            try:
                subprocess.Popen(["xdg-open", target])  # non-Windows fallback
            except Exception as e:  # noqa
                self._log(f"[open] {e}")
        except Exception as e:  # noqa
            self._log(f"[open] {e}")

    def _open_url(self, url):
        """Open a URL in the default browser (NOT via _open_path -- that would
        mangle the URL with os.path.dirname and drop the last path segment)."""
        import webbrowser
        try:
            if not webbrowser.open(url):
                os.startfile(url)
        except Exception as e:  # noqa
            self._log(f"[open] {e}")

    # ----- queue reorder ----- #
    def _move(self, direction):
        """Move selected top-level rows up (-1) or down (+1) in the queue."""
        sel = [i for i in self.tree.selection() if self.tree.parent(i) == ""]
        if not sel:
            return
        roots = list(self.tree.get_children(""))
        order = sorted((roots.index(i) for i in sel), reverse=(direction > 0))
        for idx in order:
            new = idx + direction
            if 0 <= new < len(roots):
                item = roots[idx]
                self.tree.move(item, "", new)
                roots.insert(new, roots.pop(idx))
        self._sync_task_order()
        self._save_state()

    def _sync_task_order(self):
        """Reorder self.tasks to match the tree (groups expand to their parts)."""
        ordered = []
        for top in self.tree.get_children(""):
            kids = self.tree.get_children(top)
            rows = kids if kids else (top,)
            for row in rows:
                for t in self.tasks:
                    if t.tree_id == row:
                        ordered.append(t)
        # keep any tasks not represented (shouldn't happen) at the end
        for t in self.tasks:
            if t not in ordered:
                ordered.append(t)
        self.tasks[:] = ordered

    # ----- FitGirl / generic MD5 verification ----- #
    def _verify_bins(self, tasks):
        out = os.path.join(self.dest_dir.get(), "_extracted")
        self._log(f"Verifying BIN/MD5 checksums in {out} ...")
        threading.Thread(target=self._verify_bins_worker, args=(out,), daemon=True).start()

    def _verify_bins_worker(self, extract_dir):
        try:
            ok, bad, missing = verify_md5_dir(extract_dir)
        except Exception as e:  # noqa
            self.events.put({"kind": "log", "task": None,
                             "text": f"[md5] error: {e}"})
            return
        msg = f"[md5] {ok} OK"
        if bad:
            msg += f", {len(bad)} MISMATCH ({', '.join(bad[:3])})"
        if missing:
            msg += f", {len(missing)} missing"
        if not ok and not bad and not missing:
            msg = "[md5] no .md5 checksums found in the extracted folder"
        self.events.put({"kind": "log", "task": None, "text": msg})
        if (bad or missing):
            self.events.put({"kind": "notify", "task": None, "title": "PyGet",
                             "text": f"BIN check: {len(bad)} bad, {len(missing)} missing"})
        elif ok:
            self.events.put({"kind": "notify", "task": None, "title": "PyGet",
                             "text": f"BIN check passed ({ok} file(s))"})

    # ----- clipboard auto-catch ----- #
    @staticmethod
    def _is_catchable(u):
        if is_index_page(u):
            return True
        host = urlparse(u).netloc.lower()
        if any(h in host for h in _RESOLVERS):
            return True
        frag = urlparse(u).fragment
        return bool(re.search(r"\.(rar|7z|zip|bin|iso|exe|r\d+|part\d+\.\w+)$",
                              frag, re.I))

    def _poll_clipboard(self):
        if self.watch_clipboard.get():
            try:
                data = self.clipboard_get()
            except Exception:  # noqa - clipboard empty / non-text
                data = ""
            if data and data != self._clip_last:
                self._clip_last = data
                urls = [ln.strip() for ln in data.splitlines()
                        if ln.strip().lower().startswith("http")
                        and self._is_catchable(ln.strip())]
                if urls:
                    index_pages = [u for u in urls if is_index_page(u)]
                    direct = [u for u in urls if not is_index_page(u)]
                    if direct:
                        self._queue_urls(direct)
                    for p in index_pages:
                        threading.Thread(target=self._expand_index_page,
                                         args=(p,), daemon=True).start()
                    self._log(f"Clipboard: caught {len(urls)} link(s).")
        self.after(1200, self._poll_clipboard)

    # ----- drag and drop (optional, needs tkinterdnd2) ----- #
    def _setup_dnd(self):
        try:
            import tkinterdnd2
            tkinterdnd2.TkinterDnD._require(self)  # load tkdnd into this Tk root
            self.url_text.drop_target_register(tkinterdnd2.DND_FILES,
                                               tkinterdnd2.DND_TEXT)
            self.url_text.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:  # noqa - drag-drop just unavailable without tkdnd
            pass

    def _on_drop(self, event):
        data = event.data
        # files come brace-wrapped: {C:\a b\x.txt} {C:\y.txt}
        for tok in re.findall(r"\{[^}]*\}|\S+", data):
            tok = tok.strip("{}")
            if os.path.isfile(tok):
                try:
                    with open(tok, encoding="utf-8", errors="ignore") as f:
                        self.url_text.insert("end", f.read() + "\n")
                except OSError:
                    pass
            elif tok.lower().startswith("http"):
                self.url_text.insert("end", tok + "\n")

    # ----- system tray (optional, needs pystray + Pillow) ----- #
    def _hide_to_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:  # noqa
            self.iconify()
            return False
        if self._tray is None:
            img = Image.new("RGB", (64, 64), "#1e88e5")
            ImageDraw.Draw(img).rectangle([20, 14, 44, 50], fill="white")
            menu = pystray.Menu(
                pystray.MenuItem("Open PyGet", lambda: self._restore_from_tray()),
                pystray.MenuItem("Quit", lambda: self._quit_from_tray()),
            )
            self._tray = pystray.Icon("PyGet", img, "PyGet", menu)
            threading.Thread(target=self._tray.run, daemon=True).start()
        self.withdraw()
        return True

    def _restore_from_tray(self):
        self.after(0, self.deiconify)
        self.after(0, self.lift)

    def _quit_from_tray(self):
        self.after(0, self._on_close)

    def _open_settings(self):
        SettingsDialog(self)

    def _open_fitgirl(self):
        RepackBrowser(self)

    def _extract_now(self, tasks):
        """Manually extract the selected archive(s) in the background."""
        if not SEVEN_ZIP:
            messagebox.showwarning("No 7-Zip", "7-Zip was not found, so extraction "
                                   "is unavailable.")
            return
        self._log("Extracting selected archive(s)...")
        snapshot = list(tasks)
        threading.Thread(target=self._extract_worker, args=(snapshot,), daemon=True).start()

    def _extract_worker(self, tasks):
        try:
            self.engine.extract_now(tasks)
        except Exception as e:  # noqa
            self.events.put({"kind": "log", "task": None,
                             "text": f"[extract] error: {e}"})

    def queue_game(self, game, auto_start):
        """Grab a game's links: auto-queue what we can, open gated hosts in browser."""
        self._log(f"[browse] grabbing '{game['title']}' ...")
        threading.Thread(
            target=self._grab_game, args=(game, auto_start), daemon=True,
        ).start()

    def _grab_game(self, game, auto_start):
        try:
            auto, manual = extract_download_links(game["url"])
        except Exception as e:  # noqa
            self.events.put({"kind": "log", "task": None,
                             "text": f"[browse] {game['title']}: {e}"})
            return
        if auto:
            self.events.put({"kind": "add_urls", "task": None, "urls": auto,
                             "text": f"[browse] {game['title']}: {len(auto)} link(s)",
                             "start": auto_start, "subdir": game["title"]})
        if manual:
            # Gated hosts (gofile/megadb/etc.) can't be auto-downloaded for free
            # accounts -- open each in the browser so the user finishes there.
            self.events.put({"kind": "open_urls", "task": None, "urls": manual,
                             "text": f"[browse] {game['title']}: opening "
                                     f"{len(manual)} host page(s) in your browser"})
        if not auto and not manual:
            self.events.put({"kind": "log", "task": None,
                             "text": f"[browse] {game['title']}: no supported links found"})

    # ----- queue management ----- #
    def _load_txt(self):
        path = filedialog.askopenfilename(
            filetypes=[("Text/link files", "*.txt *.crawljob *.url"), ("All", "*.*")]
        )
        if path:
            with open(path, encoding="utf-8", errors="ignore") as f:
                self.url_text.insert("end", f.read() + "\n")

    def _add_to_queue(self):
        raw = self.url_text.get("1.0", "end").strip().splitlines()
        urls = [u.strip() for u in raw if u.strip() and u.strip().lower().startswith("http")]
        if not urls:
            messagebox.showwarning("No URLs", "Paste one or more http(s) URLs first.")
            return
        # Index/release pages get scraped for file links in the background;
        # everything else is queued directly.
        index_pages = [u for u in urls if is_index_page(u)]
        direct = [u for u in urls if not is_index_page(u)]
        if direct:
            self._queue_urls(direct)
        for p in index_pages:
            self._log(f"Grabbing links from {p} ...")
            threading.Thread(
                target=self._expand_index_page, args=(p,), daemon=True
            ).start()
        self.url_text.delete("1.0", "end")

    def _queue_urls(self, urls, subdir=None):
        """
        Create tasks for new URLs (skipping duplicates), each routed into its own
        folder under the save dir: an explicit `subdir` (a game title), else the
        archive's base name for multi-part sets. Returns count added.
        """
        base = self.dest_dir.get()
        os.makedirs(base, exist_ok=True)
        existing = {t.url for t in self.tasks}
        added = 0
        for u in urls:
            if u in existing:
                continue
            name = filename_from_url(u)
            if subdir:
                folder = os.path.join(base, sanitize(subdir))
            else:
                _, label = archive_group_info(name)
                folder = os.path.join(base, sanitize(label)) if label else base
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError:
                folder = base
            t = Task(u, folder)
            self.tasks.append(t)
            self._insert_task_row(t)
            existing.add(u)
            added += 1
        if added:
            self._log(f"Queued {added} URL(s).")
            self._save_state()
        return added

    # ----- tree rows / grouping ----- #
    def _ensure_group(self, key, label):
        """Return the parent tree node for an archive group, creating it once."""
        node = self.groups.get(key)
        if node and self.tree.exists(node):
            return node
        node = self.tree.insert("", "end", text=label, open=True,
                                values=("", "", "", "", ""))
        self.groups[key] = node
        return node

    def _insert_task_row(self, t):
        """Insert a task as a child of its archive group, or as a top-level row."""
        key, label = archive_group_info(t.name)
        parent = self._ensure_group(key, label) if key else ""
        t.tree_id = self.tree.insert(parent, "end", text=t.name,
                                     values=("?", "0%", "", "", t.status))
        self._refresh_row(t)

    def _expand_index_page(self, page_url, auto_start=False, label=""):
        """(background thread) Scrape an index page and post its links to the UI."""
        try:
            links = extract_links_from_page(page_url)
        except Exception as e:  # noqa
            self.events.put({"kind": "log", "task": None,
                             "text": f"[grab] {label or page_url}: {e}"})
            return
        self.events.put({"kind": "add_urls", "task": None, "urls": links,
                         "text": f"[grab] {label}: {len(links)} link(s)" if label
                                 else f"[grab] found {len(links)} link(s)",
                         "start": auto_start})

    def _clear_queue(self):
        for item in self.tree.get_children(""):
            self.tree.delete(item)  # removes group nodes and their children
        self.tasks.clear()          # keep the same list object (engine shares it)
        self.groups = {}
        self._save_state()
        self._refresh_status()

    def _browse(self):
        d = filedialog.askdirectory()
        if d:
            self.dest_dir.set(d)

    # ----- run control ----- #
    def _start(self):
        resumable = ("Error", "HTTP", "Failed", "Resolve failed", "Retry", "Missing")
        pending = [t for t in self.tasks
                   if t.status in ("Queued", "Paused", "Connecting")
                   or t.status.startswith(resumable)]
        if not pending:
            messagebox.showinfo("Nothing to do", "Add URLs to the queue first.")
            return
        if not self._disk_space_ok(pending):
            return
        for t in pending:
            t.paused = False
            t.status = "Queued"
            self._refresh_row(t)
        self.start_btn.config(state="disabled")
        self._log(f"Starting {len(pending)} download(s)...")
        self._save_state()
        self.engine.start(
            pending, self.workers.get(), self.auto_extract.get(), self.dest_dir.get(),
            self.timeout.get(), self.max_attempts.get(), self.speed_limit.get() * 1024,
            self.connections.get(), self.delete_after_extract.get(),
        )

    def _disk_space_ok(self, pending):
        """Warn (best effort) if the target drive likely can't hold what's left."""
        need = sum(t.total - t.done for t in pending
                   if t.total and t.total > t.done)
        if not need:
            return True  # sizes unknown until we connect -- can't check yet
        fs = free_space(self.dest_dir.get())
        if fs is not None and fs < need * 1.02:
            return messagebox.askyesno(
                "Low disk space",
                f"Need about {human(need)} but only {human(fs)} is free on the "
                f"target drive.\n\nStart anyway?",
            )
        return True

    def _stop(self):
        self.engine.pause_all()
        self._log("Pausing all... (downloads resume from where they left off)")
        self.start_btn.config(state="normal")
        self._save_state()

    # ----- event pump ----- #
    def _drain_events(self):
        dirty = False
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev["kind"]
                if kind == "update":
                    self._refresh_row(ev["task"])
                    dirty = True
                elif kind == "completed":
                    self._refresh_row(ev["task"])
                    self._save_state()
                    dirty = True
                elif kind == "log":
                    self._log(ev["text"])
                elif kind == "add_urls":
                    if ev.get("text"):
                        self._log(ev["text"])
                    added = self._queue_urls(ev["urls"], ev.get("subdir"))
                    if ev.get("start") and added:
                        self._start()
                    dirty = True
                elif kind == "rechecked":
                    self._log(f"Re-check complete ({ev.get('n', 0)} file(s)).")
                    self._save_state()
                    dirty = True
                elif kind == "notify":
                    notify(ev.get("title", "PyGet"), ev.get("text", ""))
                elif kind == "open_urls":
                    if ev.get("text"):
                        self._log(ev["text"])
                    for u in ev["urls"]:
                        self._open_url(u)
                elif kind == "all_done":
                    self.start_btn.config(state="normal")
                    n_done = sum(1 for t in self.tasks
                                 if t.status in ("Done", "Extracted"))
                    self._log("All downloads finished.")
                    self._save_state()
                    if n_done:
                        notify("PyGet", f"All downloads finished ({n_done} file(s)).")
                    dirty = True
        except queue.Empty:
            pass
        if dirty:
            self._refresh_status()
        self.after(150, self._drain_events)

    def _refresh_status(self):
        n = len(self.tasks)
        if not n:
            self.status_text.set("Idle")
            self.overall["value"] = 0
            return
        done = sum(1 for t in self.tasks if t.status in ("Done", "Extracted"))
        totals = [t.total for t in self.tasks if t.total]
        total_bytes = sum(totals) if totals else 0
        done_bytes = sum(t.done for t in self.tasks)
        speed = sum(t.speed for t in self.tasks if t.speed)
        parts = [f"{done}/{n} files"]
        parts.append(f"{human(done_bytes)} / {human(total_bytes)}"
                     if total_bytes else human(done_bytes))
        if speed:
            parts.append(f"{human(speed)}/s")
            if total_bytes and total_bytes > done_bytes:
                eta = self._eta(total_bytes, done_bytes, speed)
                if eta and eta != "-":
                    parts.append(f"ETA {eta}")
        active = sum(1 for t in self.tasks if t.status == "Downloading")
        if active:
            parts.append(f"{active} active")
        self.status_text.set("    ".join(parts))
        self.overall["value"] = (done_bytes / total_bytes * 1000) if total_bytes else 0

    @staticmethod
    def _pct(done, total):
        if total:
            return f"{done * 100 // total}%"
        return human(done) if done else "0%"

    @staticmethod
    def _bar(done, total, width=10):
        """A little text progress bar for the row, e.g. '███████░░░ 72%'."""
        if total:
            filled = int(round(done / total * width)) if total else 0
            filled = max(0, min(width, filled))
            return "█" * filled + "░" * (width - filled) + f" {done * 100 // total}%"
        if done:
            return human(done)
        return "░" * width + " 0%"

    @staticmethod
    def _eta(total, done, speed):
        """ETA as H:MM:SS / M:SS, '' when finished, '-' when not estimable."""
        if total and done >= total:
            return ""
        if not total or not speed:
            return "-"
        secs = int((total - done) / speed)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _refresh_row(self, t):
        if not t.tree_id or not self.tree.exists(t.tree_id):
            return  # row was removed (e.g. user deleted the task mid-download)
        speed = f"{human(t.speed)}/s" if t.speed else ""
        self.tree.item(
            t.tree_id,
            text=t.name,
            values=(human(t.total), self._bar(t.done, t.total),
                    speed, self._eta(t.total, t.done, t.speed), t.status),
        )
        key, _ = archive_group_info(t.name)
        if key:
            self._refresh_group(key)

    def _refresh_group(self, key):
        """Roll the parts of one archive up into their parent section row."""
        node = self.groups.get(key)
        if not node or not self.tree.exists(node):
            return
        members = [t for t in self.tasks if archive_group(t.name) == key]
        if not members:
            return
        totals = [t.total for t in members if t.total]
        all_known = len(totals) == len(members)
        total = sum(totals) if all_known else None
        done = sum(t.done for t in members)
        speed = sum(t.speed for t in members if t.speed)
        n_done = sum(1 for t in members if t.status in ("Done", "Extracted"))

        statuses = [t.status for t in members]
        if any(s.startswith("Extracting") for s in statuses):
            status = next(s for s in statuses if s.startswith("Extracting"))
        elif any(s.startswith("Verifying") for s in statuses):
            status = next(s for s in statuses if s.startswith("Verifying"))
        elif any(s.startswith("Extract failed") for s in statuses):
            status = "Extract failed"
        elif n_done == len(members):
            status = "Complete"
        elif any(s.startswith(("Error", "Failed", "Resolve", "Missing", "Corrupt"))
                 for s in statuses):
            status = f"{n_done}/{len(members)} (errors)"
        elif any(s == "Downloading" for s in statuses):
            status = f"Downloading {n_done}/{len(members)}"
        else:
            status = f"{n_done}/{len(members)} done"

        bar = self._bar(done, total) if total else (human(done) if done else "")
        eta = self._eta(total, done, speed) if total else "-"
        self.tree.item(
            node,
            values=(human(total) if total else "?", bar,
                    f"{human(speed)}/s" if speed else "", eta, status),
        )

    def _log(self, text):
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")
        try:  # persist to a size-capped log file next to the app (one rotation)
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
                try:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                except OSError:
                    pass
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa
            pass


class SettingsDialog(tk.Toplevel):
    """Advanced tunables, kept out of the cramped main window."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app)
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        spins = [
            ("Timeout (seconds)", app.timeout, 5, 300, 1),
            ("Max attempts", app.max_attempts, 1, 10, 1),
            ("Speed limit (KB/s, 0 = unlimited)", app.speed_limit, 0, 1_000_000, 256),
            ("Connections per file (1 = single stream)", app.connections, 1, 16, 1),
        ]
        for i, (label, var, lo, hi, inc) in enumerate(spins):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=4)
            ttk.Spinbox(frm, from_=lo, to=hi, increment=inc, width=12,
                        textvariable=var).grid(row=i, column=1, sticky="e", pady=3)
        r = len(spins)
        ttk.Checkbutton(frm, text="Delete archive parts after a verified extract",
                        variable=app.delete_after_extract).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Checkbutton(frm, text="Minimize to system tray when window is closed",
                        variable=app.to_tray).grid(
            row=r + 1, column=0, columnspan=2, sticky="w", pady=3)
        ttk.Button(frm, text="Close", command=self._close).grid(
            row=r + 2, column=0, columnspan=2, pady=(10, 0))
        self.bind("<Escape>", lambda *_: self._close())
        self.grab_set()

    def _close(self):
        self.app._save_state()
        self.destroy()


class RepackBrowser(tk.Toplevel):
    """Browse FitGirl / DODI / SteamRIP catalogs; click a game to download it."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Browse Repacks")
        self.geometry("700x560")
        self.site = "fitgirl"
        self.page = 1
        self.query = None
        self.games = []
        self._build()
        self._load()

    def _build(self):
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Site:").pack(side="left")
        self.site_var = tk.StringVar(value=SITES[self.site][0])
        combo = ttk.Combobox(top, state="readonly", width=16,
                             values=[v[0] for v in SITES.values()],
                             textvariable=self.site_var)
        combo.pack(side="left", padx=(2, 8))
        combo.bind("<<ComboboxSelected>>", lambda *_: self._on_site())
        self.search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.search_var)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda *_: self._search())
        ttk.Button(top, text="Search", command=self._search).pack(side="left", padx=4)
        ttk.Button(top, text="Latest", command=self._latest).pack(side="left")

        self.auto_start = tk.BooleanVar(value=True)
        ttk.Checkbutton(self, text="Start downloading immediately when added "
                        "(gated hosts like gofile open in your browser)",
                        variable=self.auto_start).pack(anchor="w", padx=8)

        self.tree = ttk.Treeview(self, columns=("title",), show="headings", height=18)
        self.tree.heading("title", text="Game (double-click to download)")
        self.tree.column("title", anchor="w", width=670)
        self.tree.pack(fill="both", expand=True, padx=6, pady=4)
        self.tree.bind("<Double-1>", lambda *_: self._download())

        bottom = ttk.Frame(self, padding=6)
        bottom.pack(fill="x")
        ttk.Button(bottom, text="< Prev", command=self._prev).pack(side="left")
        self.page_lbl = ttk.Label(bottom, text="Page 1")
        self.page_lbl.pack(side="left", padx=6)
        ttk.Button(bottom, text="Next >", command=self._next).pack(side="left")
        ttk.Button(bottom, text="Download selected",
                   command=self._download).pack(side="right")

        self.status = ttk.Label(self, text="", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

    def _set_status(self, msg):
        self.status.config(text=msg)

    def _on_site(self):
        for key, (name, _) in SITES.items():
            if name == self.site_var.get():
                self.site = key
                break
        self.page = 1
        self.query = None
        self.search_var.set("")
        self._load()

    # ----- listing ----- #
    def _load(self):
        self._set_status("Loading... (first DODI/SteamRIP load can take a few seconds)")
        for i in self.tree.get_children():
            self.tree.delete(i)
        site, page, query = self.site, self.page, self.query
        fetcher = SITES[site][1]

        def work():
            try:
                games = fetcher(page, query)
            except Exception as e:  # noqa
                self.after(0, lambda: self._set_status(f"Error: {e}"))
                return
            self.after(0, lambda: self._show(games))

        threading.Thread(target=work, daemon=True).start()

    def _show(self, games):
        self.games = games
        for i in self.tree.get_children():
            self.tree.delete(i)
        for idx, g in enumerate(games):
            self.tree.insert("", "end", iid=str(idx), values=(g["title"],))
        self.page_lbl.config(text=f"Page {self.page}")
        if games:
            self._set_status(f"{len(games)} game(s)")
        elif self.site == "dodi":
            self._set_status("DODI blocked the request (Cloudflare). Try FitGirl or SteamRIP.")
        else:
            self._set_status("Nothing found (try a different search or check your connection).")

    def _search(self):
        self.query = self.search_var.get().strip() or None
        self.page = 1
        self._load()

    def _latest(self):
        self.query = None
        self.search_var.set("")
        self.page = 1
        self._load()

    def _prev(self):
        if self.page > 1:
            self.page -= 1
            self._load()

    def _next(self):
        self.page += 1
        self._load()

    def _download(self):
        sel = [i for i in self.tree.selection() if i.isdigit() and int(i) < len(self.games)]
        games = [self.games[int(i)] for i in sel]
        if not games:
            self._set_status("Select a game first.")
            return
        for g in games:
            self.app.queue_game(g, self.auto_start.get())
        self._set_status(f"Added {len(games)} game(s) to the download queue.")


def _run_selftest():
    """Headless check that 7-Zip extraction works in this build (esp. frozen exe).

    Writes results to pyget_selftest.txt next to the program and exits. Run with:
        PyGet.exe --selftest
    """
    import tempfile
    import queue as _queue
    lines = [
        f"frozen={getattr(sys, 'frozen', False)}",
        f"_MEIPASS={getattr(sys, '_MEIPASS', '-')}",
        f"SEVEN_ZIP={SEVEN_ZIP}",
        f"7z exists={bool(SEVEN_ZIP) and os.path.isfile(SEVEN_ZIP)}",
    ]
    try:
        work = tempfile.mkdtemp()
        src = os.path.join(work, "p.dat")
        with open(src, "wb") as f:
            f.write(os.urandom(1024 * 1024))
        arch = os.path.join(work, "t.7z")
        r = subprocess.run([SEVEN_ZIP, "a", arch, src],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        lines.append(f"create rc={r.returncode}")

        eng = Engine(_queue.Queue())
        t = Task("http://x/t.7z", work)
        t.name, t.path, t.status = "t.7z", arch, "Done"
        eng.tasks = [t]
        seen = []
        base_emit = eng.emit
        eng.emit = lambda kind, task=None, **e: (
            seen.append(task.status) if (kind == "update" and task) else None,
            base_emit(kind, task, **e))[1]
        eng._extract(t)
        lines += [
            f"saw_verifying={'yes' if any(s.startswith('Verifying') for s in seen) else 'no'}",
            f"saw_extracting={'yes' if any(s.startswith('Extracting') for s in seen) else 'no'}",
            f"final_status={t.status}",
            f"extracted_ok={os.path.isfile(os.path.join(work, '_extracted', 'p.dat'))}",
            "RESULT=" + ("PASS" if t.status == "Extracted" else "FAIL"),
        ]
    except Exception as e:  # noqa
        lines.append(f"ERROR={type(e).__name__}: {e}")
    with open(os.path.join(app_dir(), "pyget_selftest.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _run_selftest()
    else:
        App().mainloop()
