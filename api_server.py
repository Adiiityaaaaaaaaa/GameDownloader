"""Headless HTTP + SSE bridge exposing PyGet's download engine to the Riptide
(React/Electron) front-end.

The engine, resolvers, search and archive logic all live in ``downloader.py``;
this module reuses them without the tkinter GUI. It replicates the small amount
of orchestration glue that used to live in the ``App`` class (queueing resolved
links into tasks, starting the engine) and drains the engine's event queue into
Server-Sent Events so the UI gets live progress.

Run:  python api_server.py [--port 8787] [--dest DIR]
API:
  GET  /api/health
  GET  /api/search?q=&site=steamrip&page=1
  GET  /api/tasks
  POST /api/add            {"url": "...", "title": "..."}   (a game/repack page)
  POST /api/tasks/<id>/pause | /resume | /cancel
  POST /api/start                                           (start/resume all)
  POST /api/stop                                            (pause all)
  GET  /api/settings   |   POST /api/settings
  GET  /api/events                                          (SSE progress stream)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import queue as _queue
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

import downloader as dl


# --------------------------------------------------------------------------- #
# Game cover art. SteamRIP/FitGirl/DODI don't ship box art, so we resolve each
# title to a Steam appid (keyless) and use Steam's vertical library capsule.
# Misses return None -> the UI falls back to its generated gradient.
# --------------------------------------------------------------------------- #
_COVER_CACHE: dict[str, str | None] = {}
_COVER_LOCK = threading.Lock()
_CDN = "https://cdn.cloudflare.steamstatic.com/steam/apps/{}/library_600x900_2x.jpg"


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _clean_title(t):
    """Reduce a repack listing title to the bare game name -- for display and for
    matching against Steam. Handles SteamRIP ('... Free Download (v1.2)'),
    FitGirl ('Name - Edition, v1.2 + N DLCs') and DODI ('610- Name (v...)').
    """
    t = (t or "").strip()
    orig = t
    t = re.sub(r"\s*Free Download.*$", "", t, flags=re.I)   # SteamRIP tail
    t = re.sub(r"^\s*\d+[-.)]\s*", "", t)                     # DODI "610- " prefix
    t = re.split(r"\s+[–—-]\s+", t)[0]              # " - Edition" (spaced dash)
    # Cut at a version / build / DLC / bracket tail.
    t = re.split(r"(?:,|\s)+(?:v\d|Build\b|\(|\[|\+\s*\d|\bAll DLC)",
                 t, flags=re.I)[0]
    t = t.strip(" ,-–—")
    return t or orig


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": dl.USER_AGENT})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _pick_appid(cands, nq):
    """cands: list of (appid, name). Prefer exact, then prefix, then a two-token
    contiguous match (rejects 'Alan Walker ... Wake Up' for 'Alan Wake')."""
    for a, n in cands:
        if _norm(n) == nq:
            return a
    for a, n in cands:
        if _norm(n).startswith(nq):
            return a
    guard = " ".join(nq.split()[:2])
    for a, n in cands:
        if guard and guard in _norm(n):
            return a
    return None


_EDITION = re.compile(
    r"\b(?:ultimate|deluxe|complete|definitive|enhanced|gold|goty|premium|"
    r"standard|digital deluxe|game of the year)\s+edition\b", re.I)


def _resolve_appid(query):
    """Resolve one query string to a Steam appid via official endpoints, or None."""
    nq = _norm(query)
    if not nq:
        return None
    try:  # community app search -- precise, games only
        d = _get_json("https://steamcommunity.com/actions/SearchApps/" + quote(query))
        a = _pick_appid([(it["appid"], it.get("name", "")) for it in d], nq)
        if a:
            return a
    except Exception:  # noqa
        pass
    try:  # storefront search -- broader, guarded against false positives
        d = _get_json("https://store.steampowered.com/api/storesearch/?term="
                      + quote(query) + "&cc=us&l=en")
        items = d.get("items") or []
        return _pick_appid([(it["id"], it.get("name", "")) for it in items], nq)
    except Exception:  # noqa
        return None


def steam_cover(title):
    """Return an official Steam poster URL for `title`, or None. Cached per title.

    Art comes from Steam's public store CDN (a legitimate first-party source),
    never scraped from the repack site. Tries the cleaned name, then the name
    with any edition suffix stripped, then just the part before a ':' subtitle.
    """
    key = _clean_title(title)
    with _COVER_LOCK:
        if key in _COVER_CACHE:
            return _COVER_CACHE[key]
    variants = [key]
    noed = _EDITION.sub("", key).strip(" :,-–—")
    if noed and noed != key:
        variants.append(noed)
    if ":" in noed:
        variants.append(noed.split(":")[0].strip())
    appid = None
    for q in variants:
        appid = _resolve_appid(q)
        if appid:
            break
    url = _CDN.format(appid) if appid else None
    with _COVER_LOCK:
        _COVER_CACHE[key] = url
    return url


# --------------------------------------------------------------------------- #
# Controller: owns the engine, the task list, settings, and SSE subscribers.
# --------------------------------------------------------------------------- #
class Controller:
    def __init__(self, dest_dir=None):
        self.events = _queue.Queue()
        self.engine = dl.Engine(self.events)
        self.tasks = []                 # list[dl.Task], shared with the engine
        self.engine.tasks = self.tasks
        self._id = 0
        self._lock = threading.Lock()
        self._subs = []                 # list[queue.Queue] of SSE subscribers
        self._subs_lock = threading.Lock()

        self.settings = {
            "dest_dir": dest_dir or os.path.join(dl.app_dir(), "downloads"),
            "workers": 3,
            "connections": 4,
            "auto_extract": True,
            "stream_extract": dl.stream_extract_available(),
            "delete_after_extract": False,
            "timeout": 30,
            "max_attempts": dl.MAX_ATTEMPTS,
            "speed_limit": 0,           # KB/s, 0 = unlimited
        }
        threading.Thread(target=self._pump_events, daemon=True).start()

    # ---- SSE plumbing -------------------------------------------------- #
    def subscribe(self):
        q = _queue.Queue(maxsize=1000)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _broadcast(self, obj):
        with self._subs_lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(obj)
            except _queue.Full:
                pass

    def _pump_events(self):
        """Drain the engine's event queue -> update tasks, replicate the GUI's
        queue/start glue, and fan out SSE messages."""
        while True:
            ev = self.events.get()
            kind = ev.get("kind")
            task = ev.get("task")
            if kind in ("update", "completed"):
                self._broadcast({"type": "task", "task": self._task_json(task)})
                if kind == "completed":
                    self._broadcast({"type": "log",
                                     "text": f"[{task.name}] done"})
            elif kind == "log":
                self._broadcast({"type": "log", "text": ev.get("text", "")})
            elif kind == "add_urls":
                if ev.get("text"):
                    self._broadcast({"type": "log", "text": ev["text"]})
                self._queue_urls(ev["urls"], ev.get("subdir"))
                if ev.get("start"):
                    self.start_all()
            elif kind == "add_items":
                if ev.get("text"):
                    self._broadcast({"type": "log", "text": ev["text"]})
                self._queue_items(ev["items"], ev.get("subdir"))
                if ev.get("start"):
                    self.start_all()
            elif kind == "open_urls":
                # No browser in headless mode; surface the links so the UI can
                # tell the user these hosts need manual handling.
                self._broadcast({"type": "manual", "urls": ev.get("urls", []),
                                 "text": ev.get("text", "")})

    # ---- task helpers -------------------------------------------------- #
    def _next_id(self):
        self._id += 1
        return self._id

    def _task_json(self, t):
        total = t.total or 0
        pct = (t.done / total * 100) if total else 0
        return {
            "id": getattr(t, "id", None),
            "name": t.name,
            "url": t.url,
            "size": total,
            "done": t.done,
            "speed": t.speed,
            "status": t.status,
            "progress": round(pct, 1),
            "dest_dir": t.dest_dir,
            "stream": getattr(t, "stream", False),
            "source": _source_of(t.url),
        }

    def list_tasks(self):
        with self._lock:
            return [self._task_json(t) for t in self.tasks]

    def _queue_urls(self, urls, subdir=None):
        base = self.settings["dest_dir"]
        os.makedirs(base, exist_ok=True)
        with self._lock:
            existing = {t.url for t in self.tasks}
            for u in urls:
                if u in existing:
                    continue
                name = dl.filename_from_url(u)
                if subdir:
                    folder = os.path.join(base, dl.sanitize(subdir))
                else:
                    _, label = dl.archive_group_info(name)
                    folder = os.path.join(base, dl.sanitize(label)) if label else base
                try:
                    os.makedirs(folder, exist_ok=True)
                except OSError:
                    folder = base
                t = dl.Task(u, folder, stream=self.settings["stream_extract"])
                t.id = self._next_id()
                self.tasks.append(t)
                existing.add(u)
                self._broadcast({"type": "task", "task": self._task_json(t)})

    def _queue_items(self, items, subdir=None):
        base = self.settings["dest_dir"]
        folder = os.path.join(base, dl.sanitize(subdir)) if subdir else base
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = base
        with self._lock:
            existing = {t.url for t in self.tasks}
            for it in items:
                if it["url"] in existing:
                    continue
                t = dl.Task(it["url"], folder, name=it.get("name"),
                            headers=it.get("headers"),
                            stream=self.settings["stream_extract"])
                t.gofile_code = it.get("gofile_code")
                if it.get("size"):
                    t.total = it["size"]
                t.id = self._next_id()
                self.tasks.append(t)
                existing.add(it["url"])
                self._broadcast({"type": "task", "task": self._task_json(t)})

    # ---- public actions ------------------------------------------------ #
    def add(self, url, title=None):
        """Resolve a repack/game page and queue its download(s) (background)."""
        title = title or dl.filename_from_url(url)
        threading.Thread(target=self._grab, args=(url, title), daemon=True).start()

    def _grab(self, url, title):
        try:
            auto, expand, manual = dl.extract_download_links(url)
        except Exception as e:  # noqa
            self._broadcast({"type": "log", "text": f"[add] {title}: {e}"})
            return
        if dl.is_mirror_page(url):
            chosen = dl.best_mirror(auto, expand, manual)
            auto, expand, manual = [], [], []
            if chosen:
                kind, u = chosen
                {"auto": auto, "expand": expand, "manual": manual}[kind].append(u)
        if auto:
            self._queue_urls(auto, subdir=title)
        for g in expand:
            try:
                items = dl.gofile_expand(g)
                self._queue_items(items, subdir=title)
            except Exception as e:  # noqa
                self._broadcast({"type": "log", "text": f"[gofile] {title}: {e}"})
        if manual:
            self._broadcast({"type": "manual", "urls": manual,
                             "text": f"[add] {title}: {len(manual)} link(s) need a "
                                     "browser (captcha/premium host)"})
        if auto or expand:
            self.start_all()
        else:
            self._broadcast({"type": "log",
                             "text": f"[add] {title}: no supported links found"})

    def start_all(self):
        resumable = ("Error", "HTTP", "Failed", "Resolve failed", "Retry", "Missing")
        with self._lock:
            pending = [t for t in self.tasks
                       if t.status in ("Queued", "Paused", "Connecting")
                       or t.status.startswith(resumable)]
            for t in pending:
                t.paused = False
                t.status = "Queued"
        if not pending:
            return 0
        s = self.settings
        self.engine.start(pending, s["workers"], s["auto_extract"], s["dest_dir"],
                          s["timeout"], s["max_attempts"], s["speed_limit"] * 1024,
                          s["connections"], s["delete_after_extract"])
        return len(pending)

    def stop_all(self):
        self.engine.pause_all()

    def _find(self, task_id):
        with self._lock:
            for t in self.tasks:
                if getattr(t, "id", None) == task_id:
                    return t
        return None

    def pause(self, task_id):
        t = self._find(task_id)
        if t:
            self.engine.pause(t)
            return True
        return False

    def resume(self, task_id):
        t = self._find(task_id)
        if not t:
            return False
        t.paused = False
        t.status = "Queued"
        self.start_all()
        return True

    def cancel(self, task_id, delete=False):
        t = self._find(task_id)
        if not t:
            return False
        self.engine.pause(t)
        if delete:
            for p in (t.part_path, t.part_path + ".segs", t.part_path + ".ckpt",
                      t.path):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
        with self._lock:
            if t in self.tasks:
                self.tasks.remove(t)
        self._broadcast({"type": "removed", "id": task_id})
        return True

    def reveal(self, task_id):
        """Open the task's folder in the OS file browser (best effort)."""
        t = self._find(task_id)
        if not t:
            return False
        target = t.dest_dir
        try:
            if sys.platform == "win32" and os.path.isfile(t.path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(t.path)])
                return True
            if hasattr(os, "startfile"):
                os.startfile(target)  # noqa - Windows only
                return True
        except Exception:  # noqa
            pass
        return False

    def search(self, q, site="steamrip", page=1):
        site = site if site in dl.SITES else "steamrip"
        label = {"steamrip": "SteamRIP", "fitgirl": "FitGirl",
                 "dodi": "DODI"}.get(site, site)
        fetch = dl.SITES[site][1]
        games = fetch(page=page, query=q or None)
        return [{"title": g["title"], "clean": _clean_title(g["title"]),
                 "url": g["url"], "source": label} for g in games]


_SOURCE_HOSTS = [
    ("steamrip.com", "SteamRIP"), ("gofile", "GoFile"),
    ("buzzheavier", "BuzzHeavier"), ("bzzhr", "BuzzHeavier"),
    ("fileditch", "FileDitch"), ("pixeldrain", "PixelDrain"),
    ("datanodes", "DataNodes"), ("fuckingfast", "FuckingFast"),
]


def _source_of(url):
    low = (url or "").lower()
    for host, label in _SOURCE_HOSTS:
        if host in low:
            return label
    return "Direct"


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
CTRL: Controller | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    # ---- helpers ---- #
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path == "/api/health":
            return self._json({"ok": True, "version": "riptide-bridge",
                               "stream_extract": dl.stream_extract_available()})
        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            site = (qs.get("site") or ["steamrip"])[0]
            page = int((qs.get("page") or ["1"])[0])
            try:
                return self._json({"results": CTRL.search(q, site, page)})
            except Exception as e:  # noqa
                return self._json({"error": str(e)}, 500)
        if path == "/api/cover":
            title = (qs.get("title") or [""])[0]
            try:
                return self._json({"url": steam_cover(title)})
            except Exception:  # noqa
                return self._json({"url": None})
        if path == "/api/tasks":
            return self._json({"tasks": CTRL.list_tasks()})
        if path == "/api/settings":
            return self._json(CTRL.settings)
        if path == "/api/events":
            return self._sse()
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        body = self._body()
        if path == "/api/add":
            url = body.get("url")
            if not url:
                return self._json({"error": "url required"}, 400)
            CTRL.add(url, body.get("title"))
            return self._json({"ok": True})
        if path == "/api/start":
            return self._json({"started": CTRL.start_all()})
        if path == "/api/stop":
            CTRL.stop_all()
            return self._json({"ok": True})
        if path == "/api/settings":
            CTRL.settings.update({k: v for k, v in body.items()
                                  if k in CTRL.settings})
            return self._json(CTRL.settings)
        # /api/tasks/<id>/<action>
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks":
            try:
                tid = int(parts[2])
            except ValueError:
                return self._json({"error": "bad id"}, 400)
            action = parts[3]
            if action == "cancel":
                return self._json({"ok": CTRL.cancel(tid, bool(body.get("delete")))})
            if action == "reveal":
                return self._json({"ok": CTRL.reveal(tid)})
            fn = {"pause": CTRL.pause, "resume": CTRL.resume}.get(action)
            if fn:
                return self._json({"ok": fn(tid)})
        return self._json({"error": "not found"}, 404)

    # ---- SSE ---- #
    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._cors()
        self.end_headers()
        q = CTRL.subscribe()
        try:
            # Prime the client with the current task list.
            self._sse_send({"type": "snapshot", "tasks": CTRL.list_tasks()})
            while True:
                try:
                    msg = q.get(timeout=15)
                    self._sse_send(msg)
                except _queue.Empty:
                    self.wfile.write(b": ping\n\n")  # keep-alive comment
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            CTRL.unsubscribe(q)

    def _sse_send(self, obj):
        self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode("utf-8"))
        self.wfile.flush()


def main():
    global CTRL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--dest", default=None)
    args = ap.parse_args()
    CTRL = Controller(dest_dir=args.dest)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"[api] PyGet bridge on http://127.0.0.1:{args.port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
