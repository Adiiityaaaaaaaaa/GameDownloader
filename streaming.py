"""Streaming extraction: extract an archive straight from an HTTP response,
without ever storing the full archive on disk.

Backed by libarchive (stream-oriented; reads RAR/RAR5/ZIP/tar...). Two things
make this resumable:

  * **Extraction checkpoints** (``libarchive.checkpoint``). Each file is recorded
    as it lands on disk (atomically), so a restart never re-extracts a completed
    file and never leaves a truncated file that looks complete.

  * **Range-based transfer resume** (``HttpRangeReader``). When the server
    supports HTTP Range requests we hand libarchive a *seekable* view of the URL.
    libarchive then jumps over the data of already-extracted entries instead of
    reading it — and because our reader turns those jumps into new Range requests
    (or simply repositions), the skipped bytes are **never downloaded**. So a
    resumed job continues the transfer from roughly where it stopped rather than
    re-downloading from the start.

Random access only helps when the archive layout allows it:
  * **Non-solid archives** (each file compressed independently) — e.g. ZIP, and
    RAR built without solid mode — resume the transfer from the first unfinished
    file. Only the remaining files are downloaded.
  * **Solid archives / whole-stream compression** (gzip/xz-compressed tar, RAR
    solid mode) can't be decoded from the middle, so resuming them re-reads from
    the start. This is detected automatically (a decode/CRC error on the seekable
    attempt) and falls back to a correct forward re-read; completed files are
    still not re-extracted.
  * If the server doesn't support Range at all, we fall back to a single forward
    stream (re-reads from the start, like before).

Requires the native libarchive shared library. On Windows it isn't on PATH by
default, so we probe a few known locations (MinGW / Git-for-Windows) and point
the binding at it before import. Resumable extraction also needs our patched
binding (adds ``libarchive.checkpoint`` + a skip callback); a stock
``libarchive-c`` is treated as unavailable.
"""

from __future__ import annotations

import http.client
import os
import re
import sys

import requests

try:
    import urllib3
    _URLLIB3_ERRORS = (urllib3.exceptions.HTTPError,)
except Exception:  # noqa: BLE001 - urllib3 ships with requests, but be defensive
    _URLLIB3_ERRORS = ()

# Transient transfer failures worth retrying (as opposed to a corrupt archive or
# a login wall). Exposed so the manager can classify errors for its retry loop.
NETWORK_ERRORS = (
    requests.RequestException, http.client.HTTPException, OSError,
) + _URLLIB3_ERRORS

_lib = None          # the imported libarchive module, or None
_load_error: str | None = None

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) rar-downloader"


def _locate_native_lib() -> None:
    """Make the native libarchive DLL discoverable (Windows), if we can find one."""
    if os.name != "nt" or os.environ.get("LIBARCHIVE"):
        return
    # PyInstaller unpacks bundled data to sys._MEIPASS; a source run looks next to
    # this file. Prefer our vendored copy so the app is self-contained, then fall
    # back to a system MinGW / Git-for-Windows install.
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(here, "native"),
        here,
        os.path.join(os.path.expanduser("~"), "toolchains", "mingw64", "bin"),
        r"C:\Program Files\Git\mingw64\bin",
        r"C:\Program Files\Git\usr\bin",
        r"C:\msys64\mingw64\bin",
    ]
    names = ("libarchive-13.dll", "libarchive.dll", "archive.dll")
    for d in candidates:
        for name in names:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                # libarchive's own dependencies (libz, liblzma, libzstd, ...) sit
                # in the same folder. Since Python 3.8, Windows no longer resolves
                # a DLL's dependencies via PATH, so add_dll_directory is required;
                # keep the PATH update too for older/other loaders.
                try:
                    os.add_dll_directory(d)
                except (OSError, AttributeError):
                    pass
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                os.environ["LIBARCHIVE"] = p
                return


def _load():
    global _lib, _load_error
    if _lib is not None or _load_error is not None:
        return
    try:
        _locate_native_lib()
        import libarchive  # noqa: PLC0415 - deliberately lazy
        # Resumable extraction needs our patched binding (libarchive.checkpoint).
        # A stock libarchive-c imports fine but lacks it; treat that as
        # unavailable so the GUI disables stream-extract instead of crashing.
        if not hasattr(libarchive, "checkpoint_stream_extract"):
            raise RuntimeError(
                "installed libarchive binding lacks checkpoint support; "
                "install the patched clone (pip install -e ../libarchive-c)"
            )
        _lib = libarchive
    except Exception as exc:  # noqa: BLE001
        _load_error = str(exc)


def is_available() -> bool:
    _load()
    return _lib is not None


def unavailable_reason() -> str:
    _load()
    return _load_error or "libarchive not found"


class _Aborted(Exception):
    pass


# Expose the abort exception for callers that want to catch it distinctly.
Aborted = _Aborted


class _HtmlResponse(Exception):
    """Raised when the URL serves a web page (login wall) instead of a file."""


_HTML_MSG = (
    "Server returned a web page, not a file - this link likely needs a "
    "login/session. Paste the required Cookie header."
)


class HttpRangeReader:
    """A seekable file-like view of a URL, backed by HTTP Range requests.

    Presents ``readinto`` / ``seek`` / ``tell`` / ``skip`` so libarchive can
    treat the URL as a seekable file. Sequential reads stream from a single
    kept-open GET; a ``seek``/``skip`` abandons it so the *next* read reopens a
    Range request at the new offset — which is how skipped byte ranges avoid
    being downloaded at all.

    ``on_bytes`` reports bytes that actually crossed the network (not archive
    position, which libarchive moves around unpredictably — e.g. seeking to a
    ZIP's central directory at the end). So on a resumed non-solid archive the
    callback only fires for the portion that truly had to be downloaded.
    """

    def __init__(self, url, headers=None, on_bytes=None, should_abort=None,
                 allow_range=True, timeout=30):
        self.url = url
        self._session = requests.Session()
        self._base_headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
        if headers:
            self._base_headers.update(headers)
        self._on_bytes = on_bytes
        self._should_abort = should_abort
        self._timeout = timeout

        self.total = None            # full resource size, if known
        self.supports_range = False
        self.html = False
        self.aborted = False
        self.io_error = None         # last network error hit mid-read, if any
        self.fetched = 0             # bytes actually downloaded
        self.requests = 0           # number of HTTP GETs issued

        self._pos = 0                # logical read position
        self._resp = None            # current streaming response
        self._raw = None
        self._stream_at = -1         # position the current response is reading at

        self._probe(allow_range)

    # ---- setup ---------------------------------------------------------

    def _probe(self, allow_range):
        """Open the resource once: detect Range support, size, and HTML walls.

        The opened response is kept as the stream positioned at byte 0, so a
        fresh (non-resumed) download needs no extra request.
        """
        hdrs = dict(self._base_headers)
        if allow_range:
            hdrs["Range"] = "bytes=0-"
        resp = self._session.get(self.url, headers=hdrs, stream=True,
                                 timeout=self._timeout)
        resp.raise_for_status()
        self.requests += 1
        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype.startswith("text/html"):
            self.html = True
        if allow_range and resp.status_code == 206:
            self.supports_range = True
            m = re.search(r"/(\d+)\s*$", resp.headers.get("Content-Range", ""))
            if m:
                self.total = int(m.group(1))
        else:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                self.total = int(cl)
        self._resp = resp
        self._raw = resp.raw
        self._stream_at = 0

    # ---- file-like interface ------------------------------------------

    def readinto(self, buf) -> int:
        if self._should_abort and self._should_abort():
            self.aborted = True
            return 0
        if self.total is not None and self._pos >= self.total:
            return 0
        # A network failure here happens inside libarchive's ctypes callback,
        # which swallows exceptions — so record it and signal EOF; the caller
        # inspects io_error afterwards and turns it into a retryable error.
        try:
            self._ensure_open()
            if self._raw is None:
                return 0
            view = memoryview(buf).cast("B")
            data = self._raw.read(len(view))
        except NETWORK_ERRORS as exc:
            self.io_error = exc
            return 0
        if not data:
            return 0
        n = len(data)
        view[:n] = data
        self._pos += n
        self._stream_at += n
        self.fetched += n
        if self._on_bytes:
            self._on_bytes(n)
        return n

    def seek(self, offset, whence=0) -> int:
        if whence == os.SEEK_SET:
            newpos = offset
        elif whence == os.SEEK_CUR:
            newpos = self._pos + offset
        elif whence == os.SEEK_END:
            newpos = (self.total or 0) + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        self._pos = max(0, newpos)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return self.supports_range

    def skip(self, request) -> int:
        """libarchive skip callback: jump `request` bytes forward, no download."""
        if not self.supports_range or request <= 0:
            return 0
        newpos = self._pos + request
        if self.total is not None and newpos > self.total:
            newpos = self.total
        skipped = newpos - self._pos
        self._pos = newpos
        return skipped

    def close(self) -> None:
        self._close_resp()
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    # ---- internals -----------------------------------------------------

    def _ensure_open(self) -> None:
        if self._resp is not None and self._stream_at == self._pos:
            return
        self._close_resp()
        self._open_at(self._pos)

    def _open_at(self, pos) -> None:
        hdrs = dict(self._base_headers)
        if self.supports_range:
            hdrs["Range"] = f"bytes={pos}-"
        resp = self._session.get(self.url, headers=hdrs, stream=True,
                                 timeout=self._timeout)
        if resp.status_code == 416:  # requested past end of resource
            resp.close()
            return
        # A server that silently ignores Range and restarts from 0 would corrupt
        # a mid-stream read; treat that as fatal so the caller can fall back.
        if self.supports_range and pos > 0 and resp.status_code == 200:
            resp.close()
            raise OSError("server ignored Range request on resume")
        resp.raise_for_status()
        self.requests += 1
        self._resp = resp
        self._raw = resp.raw
        self._stream_at = pos

    def _close_resp(self) -> None:
        if self._resp is not None:
            try:
                self._resp.close()
            except Exception:  # noqa: BLE001
                pass
        self._resp = None
        self._raw = None
        self._stream_at = -1


def download_extract(
    url, out_dir, checkpoint_path, *, headers=None, on_bytes=None,
    should_abort=None, on_total=None, source=None, stats=None,
) -> list:
    """Download+extract an archive from `url` into `out_dir`, resumably.

    Uses a seekable Range-backed reader when the server supports it, so a resumed
    job downloads only the not-yet-extracted portion (for randomly-accessible
    archive layouts). Falls back to a correct forward re-read otherwise.

    ``on_total(size)``  called once when the full size is known
    ``on_bytes(n)``     called with bytes downloaded (network), plus a one-time
                        seed at resume so a progress bar starts near where it left
    ``should_abort()``  polled between reads; True raises Aborted
    ``source``          opaque identity (URL); guards against a stale checkpoint
    ``stats``           optional dict; filled with fetched/requests/supports_range
                        /fell_back for observability and testing

    Returns the list of extracted file paths. Raises Aborted on pause/cancel.
    """
    _load()
    if _lib is None:
        raise RuntimeError(unavailable_reason())

    if stats is not None:
        stats.setdefault("fetched", 0)
        stats.setdefault("requests", 0)
        stats["fell_back"] = False

    reader = HttpRangeReader(url, headers=headers, on_bytes=on_bytes,
                             should_abort=should_abort)
    primary_accounted = False
    try:
        if reader.html:
            raise ValueError(_HTML_MSG)
        if on_total and reader.total is not None:
            on_total(reader.total)

        checkpoint = _lib.Checkpoint.load(checkpoint_path, source=source)
        # Seed a resuming progress bar at the point already extracted, so it
        # starts near where it stopped instead of jumping back to 0.
        if on_bytes and checkpoint.resume_offset:
            on_bytes(checkpoint.resume_offset)
        if stats is not None:
            stats["supports_range"] = reader.supports_range

        try:
            written = _lib.checkpoint_stream_extract(reader, out_dir, checkpoint)
        except BaseException as exc:  # noqa: BLE001
            if reader.aborted:
                raise _Aborted
            # A dropped connection surfaces here as a generic ArchiveError (the
            # real error was swallowed by the ctypes callback). Recover it so the
            # caller can retry the transfer instead of giving up.
            if reader.io_error is not None:
                raise reader.io_error
            # A decode/CRC failure on the seekable attempt means this archive
            # can't be read from the middle (solid / whole-stream compression).
            # Re-extract correctly with a plain forward re-read from the start;
            # completed files are still skipped via the checkpoint.
            if reader.supports_range and isinstance(exc, _lib.ArchiveError):
                if stats is not None:
                    stats["fetched"] += reader.fetched
                    stats["requests"] += reader.requests
                    stats["fell_back"] = True
                primary_accounted = True
                reader.close()
                return _forward_extract(
                    url, out_dir, checkpoint_path, headers=headers,
                    on_bytes=on_bytes, should_abort=should_abort,
                    on_total=on_total, source=source, stats=stats,
                )
            raise
        if reader.aborted:
            raise _Aborted
        if reader.io_error is not None:
            raise reader.io_error
        return written
    finally:
        if stats is not None and not primary_accounted:
            stats["fetched"] += reader.fetched
            stats["requests"] += reader.requests
        reader.close()


def _forward_extract(
    url, out_dir, checkpoint_path, *, headers=None, on_bytes=None,
    should_abort=None, on_total=None, source=None, stats=None,
) -> list:
    """Correctness fallback: extract from a single forward stream (no seeking)."""
    reader = HttpRangeReader(url, headers=headers, on_bytes=on_bytes,
                             should_abort=should_abort, allow_range=False)
    try:
        if reader.html:
            raise ValueError(_HTML_MSG)
        if on_total and reader.total is not None:
            on_total(reader.total)
        checkpoint = _lib.Checkpoint.load(checkpoint_path, source=source)
        try:
            written = _lib.checkpoint_stream_extract(reader, out_dir, checkpoint)
        except BaseException:  # noqa: BLE001
            if reader.aborted:
                raise _Aborted
            if reader.io_error is not None:
                raise reader.io_error
            raise
        if reader.aborted:
            raise _Aborted
        if reader.io_error is not None:
            raise reader.io_error
        return written
    finally:
        if stats is not None:
            stats["fetched"] += reader.fetched
            stats["requests"] += reader.requests
        reader.close()
