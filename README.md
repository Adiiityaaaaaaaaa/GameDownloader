# PyGet — Download Manager

A JDownloader-style GUI download manager (Tkinter), tuned for large multi-part
game repacks. Paste direct links, host pages, or a repack page and it resolves,
downloads, verifies, and extracts — with a built-in repack browser.

## Features

- **Parallel + resumable downloads** with HTTP Range, auto-retry + backoff, and
  `.part` temp files renamed atomically on completion.
- **Optional segmented (multi-connection) downloads** per file.
- **Host link resolution** — paste a `fuckingfast.co` page and it pulls the real
  direct link past the Cloudflare gate (via `curl_cffi`, with a headless-browser
  fallback when `playwright` is installed). Also: `pixeldrain`, `datanodes`.
- **Repack browser** — browse/search **FitGirl**, **DODI**, **SteamRIP** catalogs
  and one-click a game. FitGirl auto-downloads its fuckingfast links; SteamRIP
  auto-downloads via its FileDitch / buzzheavier mirrors (one mirror per game,
  not all three); remaining gated hosts open in your browser to finish there.
- **Multi-part archive grouping** in the UI with per-group + per-file ETA and a
  visual progress bar.
- **Auto-extract** completed archives with 7-Zip (bundled in the release exe),
  integrity-verified first, run hidden with live progress; optional delete of the
  archive parts afterwards.
- **MD5 verification** of extracted BIN files.
- **Quality-of-life** — persistent queue + settings, clipboard auto-catch,
  drag-and-drop, queue reorder, system-tray minimize, desktop notifications,
  per-task right-click menu (pause/resume/retry/re-check/remove), disk-space
  pre-check, speed limit, and a rotating log file.

## Run

```
pip install -r requirements.txt
python downloader.py
```

7-Zip must be installed for extraction (or use the bundled-7-Zip release build —
see [BUILD.txt](BUILD.txt)).

## Notes / limitations

- **SteamRIP** pages offer the same game on three mirrors (FileDitch,
  buzzheavier `bzzhr.to`, and gofile). The app picks one — preferring FileDitch,
  then buzzheavier — and resolves it to a direct, resumable link. gofile is used
  only as a last resort, since its folders can require a paid account.
- **DODI** sits behind a Cloudflare gate that blocks automated requests
  (including headless browsers).
- For downloads you have the right to download only.
