import { useEffect, useMemo, useRef, useState } from "react";
import {
  Search, Download, Pause, Play, X, Check, Settings, Library, Compass,
  ListChecks, HardDrive, Zap, Cpu, Wifi, Filter, Minus, Square, Gamepad2, Loader2,
} from "lucide-react";
import {
  pyget, fmtBytes, fmtSpeed, fmtEta, type Task, type SearchResult,
} from "./lib/pyget";

// Deterministic cover hue from a title (no cover art from the source, so we
// generate a stable gradient identity per game -- keeps Riptide's look).
function hueOf(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
  return h;
}

function Cover({ title, hue }: { title: string; hue: number }) {
  const [poster, setPoster] = useState<string | null>(null);
  useEffect(() => {
    let ok = true;
    pyget.cover(title).then((u) => { if (ok) setPoster(u); });
    return () => { ok = false; };
  }, [title]);
  return (
    <div
      className="relative aspect-[3/4] w-full overflow-hidden rounded-md border border-border"
      style={{
        background: `
          radial-gradient(120% 80% at 20% 10%, oklch(0.75 0.22 ${hue}) 0%, transparent 55%),
          radial-gradient(90% 70% at 90% 100%, oklch(0.55 0.24 ${(hue + 60) % 360}) 0%, transparent 60%),
          linear-gradient(160deg, oklch(0.22 0.05 ${hue}), oklch(0.14 0.02 ${(hue + 200) % 360}))`,
      }}
    >
      {poster && (
        <img
          src={poster}
          alt={title}
          loading="lazy"
          onError={() => setPoster(null)}
          className="absolute inset-0 h-full w-full object-cover"
        />
      )}
      {!poster && <div className="absolute inset-0 grid-scan opacity-40" />}
      {!poster && (
        <div className="absolute inset-x-0 bottom-0 p-3">
          <div className="text-mono text-[10px] uppercase tracking-widest text-white/70">// repack</div>
          <div className="mt-1 line-clamp-2 font-sans text-sm font-semibold leading-tight text-white drop-shadow">{title}</div>
        </div>
      )}
      <div className="absolute right-2 top-2 rounded-sm bg-black/50 px-1.5 py-0.5 text-mono text-[10px] text-primary backdrop-blur">
        RIP-READY
      </div>
    </div>
  );
}

function SourcePill({ s }: { s: string }) {
  const map: Record<string, string> = {
    SteamRIP: "text-primary border-primary/40 bg-primary/10",
    FitGirl: "text-secondary border-secondary/40 bg-secondary/10",
    DODI: "text-accent border-accent/40 bg-accent/10",
    BuzzHeavier: "text-accent border-accent/40 bg-accent/10",
    GoFile: "text-secondary border-secondary/40 bg-secondary/10",
    FileDitch: "text-warning border-warning/40 bg-warning/10",
  };
  return (
    <span className={`inline-flex items-center rounded-sm border px-1.5 py-0.5 text-mono text-[10px] uppercase tracking-wider ${map[s] ?? "text-muted-foreground border-border bg-surface-2"}`}>
      {s}
    </span>
  );
}

const DONE = new Set(["Done", "Extracted"]);
const ACTIVE = new Set(["Downloading", "Connecting", "Verifying", "Extracting"]);
function statusColor(s: string) {
  if (s === "Downloading" || s === "Connecting") return "bg-primary shadow-[0_0_10px] shadow-primary";
  if (DONE.has(s)) return "bg-success";
  if (s === "Paused") return "bg-warning";
  if (s.startsWith("Error") || s.startsWith("Failed")) return "bg-destructive";
  return "bg-muted-foreground";
}
function barColor(s: string) {
  if (DONE.has(s)) return "bg-success";
  if (s === "Paused") return "bg-warning";
  if (s.startsWith("Error") || s.startsWith("Failed")) return "bg-destructive";
  return "bg-primary";
}

const SITES = [
  { id: "steamrip", label: "SteamRIP" },
  { id: "fitgirl", label: "FitGirl" },
  { id: "dodi", label: "DODI" },
] as const;
type SiteId = (typeof SITES)[number]["id"];

export default function Index() {
  const [tab, setTab] = useState<"browse" | "queue" | "library">("browse");
  const [site, setSite] = useState<SiteId>("steamrip");
  const [q, setQ] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [tasks, setTasks] = useState<Record<number, Task>>({});
  const [log, setLog] = useState<{ t: string; text: string }[]>([]);
  const [online, setOnline] = useState(false);
  const [adding, setAdding] = useState<Record<string, boolean>>({});
  const searchTimer = useRef<number | undefined>(undefined);

  // Live event stream from the backend.
  useEffect(() => {
    let alive = true;
    pyget.health().then(() => alive && setOnline(true)).catch(() => setOnline(false));
    const stop = pyget.events({
      onSnapshot: (ts) => setTasks(Object.fromEntries(ts.map((t) => [t.id, t]))),
      onTask: (t) => setTasks((m) => ({ ...m, [t.id]: t })),
      onRemoved: (id) => setTasks((m) => { const n = { ...m }; delete n[id]; return n; }),
      onLog: (text) => setLog((l) => [{ t: nowClock(), text }, ...l].slice(0, 60)),
      onManual: (_u, text) => setLog((l) => [{ t: nowClock(), text }, ...l].slice(0, 60)),
    });
    return () => { alive = false; stop(); };
  }, []);

  // Debounced search. Empty query shows the source's default catalog listing,
  // so Browse is populated without typing.
  useEffect(() => {
    window.clearTimeout(searchTimer.current);
    setSearching(true);
    const delay = q.trim() ? 350 : 0;
    searchTimer.current = window.setTimeout(() => {
      pyget.search(q, site).then((r) => setResults(r)).catch(() => setResults([]))
        .finally(() => setSearching(false));
    }, delay);
    return () => window.clearTimeout(searchTimer.current);
  }, [q, site]);

  const taskList = useMemo(
    () => Object.values(tasks).sort((a, b) => a.id - b.id),
    [tasks],
  );
  const totalDown = taskList.filter((t) => t.status === "Downloading")
    .reduce((s, t) => s + t.speed, 0);
  const activeCount = taskList.filter((t) => ACTIVE.has(t.status)).length;
  const doneCount = taskList.filter((t) => DONE.has(t.status)).length;

  async function addGame(r: SearchResult) {
    setAdding((m) => ({ ...m, [r.url]: true }));
    try {
      await pyget.add(r.url, r.title);
      setTab("queue");
    } finally {
      setTimeout(() => setAdding((m) => ({ ...m, [r.url]: false })), 1500);
    }
  }

  return (
    <div className="min-h-screen text-foreground">
      {/* Window chrome */}
      <div className="flex h-9 items-center justify-between border-b border-border bg-surface/60 px-3 backdrop-blur">
        <div className="flex items-center gap-2 text-mono text-xs text-muted-foreground">
          <span className={`inline-block h-2 w-2 rounded-full ${online ? "bg-primary shadow-[0_0_8px] shadow-primary" : "bg-destructive"}`} />
          RIPTIDE.exe — <span className="text-foreground/80">PyGet bridge</span>
          <span className="mx-2 opacity-40">|</span>
          <span>{online ? "engine online" : "connecting…"}</span>
        </div>
        <div className="flex items-center gap-1">
          {[Minus, Square, X].map((I, i) => (
            <button key={i} className="grid h-6 w-8 place-items-center rounded-sm text-muted-foreground hover:bg-surface-2 hover:text-foreground">
              <I className="h-3.5 w-3.5" />
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-[220px_1fr_320px] gap-0">
        {/* Sidebar */}
        <aside className="min-h-[calc(100vh-2.25rem)] border-r border-border bg-surface/40 p-3">
          <div className="mb-6 flex items-center gap-2 px-2">
            <div className="grid h-9 w-9 place-items-center rounded-md bg-primary text-primary-foreground glow-primary">
              <Gamepad2 className="h-5 w-5" />
            </div>
            <div>
              <div className="font-sans text-sm font-bold tracking-tight">RIPTIDE</div>
              <div className="text-mono text-[10px] uppercase tracking-widest text-muted-foreground">game downloader</div>
            </div>
          </div>

          <nav className="space-y-1">
            {[
              { id: "browse", label: "Browse", icon: Compass, count: results.length || "" },
              { id: "queue", label: "Downloads", icon: ListChecks, count: taskList.length || "" },
              { id: "library", label: "Library", icon: Library, count: doneCount || "" },
            ].map(({ id, label, icon: I, count }) => (
              <button
                key={id}
                onClick={() => setTab(id as typeof tab)}
                className={`group flex w-full items-center justify-between rounded-md px-2.5 py-2 text-sm transition ${
                  tab === id ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-surface-2 hover:text-foreground"
                }`}
              >
                <span className="flex items-center gap-2.5"><I className="h-4 w-4" />{label}</span>
                <span className="text-mono text-[10px] opacity-70">{count}</span>
              </button>
            ))}
          </nav>

          <div className="mt-6 px-2 text-mono text-[10px] uppercase tracking-widest text-muted-foreground">Sources</div>
          <div className="mt-2 space-y-1">
            {SITES.map((s) => (
              <button
                key={s.id}
                onClick={() => { setSite(s.id); setTab("browse"); }}
                className={`flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-sm transition ${
                  site === s.id ? "bg-primary/15 text-primary" : "text-muted-foreground hover:bg-surface-2 hover:text-foreground"
                }`}
              >
                <span>{s.label}</span>
                {site === s.id && <span className="h-1.5 w-1.5 rounded-full bg-primary shadow-[0_0_8px] shadow-primary" />}
              </button>
            ))}
          </div>

          <div className="mt-6 rounded-md border border-border bg-surface-2/50 p-3">
            <div className="text-mono text-[10px] uppercase tracking-widest text-muted-foreground">Active transfers</div>
            <div className="mt-1 flex items-baseline gap-1">
              <span className="text-mono text-xl font-bold text-primary">{activeCount}</span>
              <span className="text-mono text-xs text-muted-foreground">/ {taskList.length} queued</span>
            </div>
            <div className="mt-2 flex items-center gap-1.5 text-mono text-[10px] text-muted-foreground">
              <HardDrive className="h-3 w-3" /> {fmtSpeed(totalDown)}
            </div>
          </div>

          <button className="mt-6 flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-sm text-muted-foreground hover:bg-surface-2 hover:text-foreground">
            <Settings className="h-4 w-4" /> Settings
          </button>
        </aside>

        {/* Main */}
        <main className="min-h-[calc(100vh-2.25rem)] p-6">
          <div className="flex items-center gap-2">
            <div className="flex flex-1 items-center gap-2 rounded-md border border-border bg-surface/60 px-3 py-2 focus-within:border-primary">
              <Search className="h-4 w-4 text-muted-foreground" />
              <input
                value={q}
                autoFocus
                onChange={(e) => { setQ(e.target.value); setTab("browse"); }}
                placeholder={`Search ${SITES.find((s) => s.id === site)?.label}'s catalog…`}
                className="w-full bg-transparent text-sm outline-none placeholder:text-muted-foreground"
              />
              {searching && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
            </div>
            <button className="flex items-center gap-1.5 rounded-md border border-border bg-surface px-3 py-2 text-sm hover:bg-surface-2">
              <Filter className="h-4 w-4" /> Filters
            </button>
          </div>

          {tab === "browse" && (
            <>
              <div className="mt-6 flex items-baseline justify-between">
                <h2 className="text-mono text-xs uppercase tracking-widest text-muted-foreground">
                  {q ? `"${q}" · ${results.length} results`
                     : `${SITES.find((s) => s.id === site)?.label} · latest`}
                </h2>
              </div>
              {results.length === 0 && !searching && (
                <div className="mt-16 grid place-items-center text-center text-muted-foreground">
                  <Zap className="h-8 w-8 opacity-40" />
                  <p className="mt-3 text-sm">{q ? "No matches." : `Search ${SITES.find((s) => s.id === site)?.label} — pick a source in the sidebar.`}</p>
                </div>
              )}
              <div className="mt-3 grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
                {results.map((g) => (
                  <div key={g.url} className="group cursor-pointer" onClick={() => addGame(g)}>
                    <div className="relative">
                      <Cover title={cleanTitle(g.title)} hue={hueOf(g.title)} />
                      <button className="absolute inset-x-2 bottom-2 flex translate-y-2 items-center justify-center gap-1.5 rounded-sm bg-primary py-1.5 text-mono text-[11px] font-semibold uppercase tracking-widest text-primary-foreground opacity-0 transition group-hover:translate-y-0 group-hover:opacity-100">
                        {adding[g.url] ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Download className="h-3.5 w-3.5" />}
                        {adding[g.url] ? "queued" : "download"}
                      </button>
                    </div>
                    <div className="mt-2 flex items-center justify-between gap-2">
                      <div className="min-w-0">
                        <div className="truncate font-sans text-sm font-medium">{cleanTitle(g.title)}</div>
                        <div className="text-mono text-[10px] text-muted-foreground">{g.source}</div>
                      </div>
                      <SourcePill s={g.source} />
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}

          {tab === "queue" && (
            <section className="mt-6 overflow-hidden rounded-lg border border-border bg-surface">
              <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
                <div className="text-mono text-[10px] uppercase tracking-widest text-muted-foreground">Download queue · {taskList.length}</div>
                <div className="flex gap-2">
                  <button onClick={() => pyget.start()} className="flex items-center gap-1.5 rounded-sm bg-primary px-2.5 py-1 text-mono text-[11px] font-semibold uppercase tracking-widest text-primary-foreground"><Play className="h-3 w-3" /> Start all</button>
                  <button onClick={() => pyget.stop()} className="flex items-center gap-1.5 rounded-sm border border-border bg-surface-2 px-2.5 py-1 text-mono text-[11px] uppercase tracking-widest hover:bg-surface"><Pause className="h-3 w-3" /> Pause all</button>
                </div>
              </div>
              {taskList.length === 0 && (
                <div className="grid place-items-center py-16 text-sm text-muted-foreground">Queue is empty — add a game from Browse.</div>
              )}
              {taskList.map((r) => (
                <div key={r.id} className="grid grid-cols-[1fr_110px_150px_110px_90px_84px] items-center gap-4 border-b border-border/60 px-4 py-3 hover:bg-surface-2/60">
                  <div className="flex items-center gap-3 min-w-0">
                    <span className={`inline-block h-2 w-2 shrink-0 rounded-full ${statusColor(r.status)}`} />
                    <div className="min-w-0">
                      <div className="truncate font-medium">{r.name}</div>
                      <div className="text-mono text-[10px] text-muted-foreground">
                        {fmtBytes(r.done)} / {r.size ? fmtBytes(r.size) : "?"}{r.stream ? " · stream" : ""}
                      </div>
                    </div>
                  </div>
                  <div><SourcePill s={r.source} /></div>
                  <div>
                    <div className="h-1.5 w-full overflow-hidden rounded-full bg-background">
                      <div className={`h-full rounded-full ${barColor(r.status)}`} style={{ width: `${r.progress}%` }} />
                    </div>
                    <div className="text-mono mt-1 text-[10px] text-muted-foreground">{r.progress.toFixed(0)}% · {r.status}</div>
                  </div>
                  <div className="text-mono text-xs text-foreground">{fmtSpeed(r.speed)}</div>
                  <div className="text-mono text-xs text-muted-foreground">{fmtEta(r.size, r.done, r.speed)}</div>
                  <div className="flex items-center justify-end gap-1">
                    {DONE.has(r.status) ? (
                      <span className="grid h-7 w-7 place-items-center text-success"><Check className="h-3.5 w-3.5" /></span>
                    ) : r.status === "Paused" ? (
                      <button onClick={() => pyget.resume(r.id)} className="grid h-7 w-7 place-items-center rounded-sm text-primary hover:bg-primary/15"><Play className="h-3.5 w-3.5" /></button>
                    ) : (
                      <button onClick={() => pyget.pause(r.id)} className="grid h-7 w-7 place-items-center rounded-sm text-warning hover:bg-warning/15"><Pause className="h-3.5 w-3.5" /></button>
                    )}
                    <button onClick={() => pyget.cancel(r.id)} className="grid h-7 w-7 place-items-center rounded-sm text-muted-foreground hover:bg-surface-2"><X className="h-3.5 w-3.5" /></button>
                  </div>
                </div>
              ))}
            </section>
          )}

          {tab === "library" && (
            <section className="mt-6 grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
              {taskList.filter((t) => DONE.has(t.status)).map((g) => (
                <div key={g.id} className="rounded-md border border-border bg-surface p-3">
                  <Cover title={g.name} hue={hueOf(g.name)} />
                  <div className="mt-3 flex items-center justify-between">
                    <div className="text-mono text-[10px] uppercase tracking-widest text-success">● installed</div>
                    <span className="text-mono text-[10px] text-muted-foreground">{fmtBytes(g.size)}</span>
                  </div>
                </div>
              ))}
              {taskList.filter((t) => DONE.has(t.status)).length === 0 && (
                <div className="col-span-full grid place-items-center py-16 text-sm text-muted-foreground">Nothing finished yet.</div>
              )}
            </section>
          )}
        </main>

        {/* Right rail: live monitor */}
        <aside className="min-h-[calc(100vh-2.25rem)] border-l border-border bg-surface/40 p-4">
          <div className="text-mono text-[10px] uppercase tracking-widest text-muted-foreground">Live monitor</div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <div className="rounded-md border border-border bg-surface-2/50 p-3">
              <div className="flex items-center gap-1.5 text-mono text-[10px] uppercase text-muted-foreground"><Wifi className="h-3 w-3" /> Down</div>
              <div className="text-mono mt-1 text-lg font-bold text-primary">{fmtSpeed(totalDown)}</div>
            </div>
            <div className="rounded-md border border-border bg-surface-2/50 p-3">
              <div className="flex items-center gap-1.5 text-mono text-[10px] uppercase text-muted-foreground"><ListChecks className="h-3 w-3" /> Active</div>
              <div className="text-mono mt-1 text-lg font-bold text-accent">{activeCount}</div>
            </div>
            <div className="rounded-md border border-border bg-surface-2/50 p-3">
              <div className="flex items-center gap-1.5 text-mono text-[10px] uppercase text-muted-foreground"><Check className="h-3 w-3" /> Done</div>
              <div className="text-mono mt-1 text-lg font-bold">{doneCount}</div>
            </div>
            <div className="rounded-md border border-border bg-surface-2/50 p-3">
              <div className="flex items-center gap-1.5 text-mono text-[10px] uppercase text-muted-foreground"><Cpu className="h-3 w-3" /> Queued</div>
              <div className="text-mono mt-1 text-lg font-bold">{taskList.length}</div>
            </div>
          </div>

          <div className="text-mono mt-5 text-[10px] uppercase tracking-widest text-muted-foreground">Log</div>
          <div className="text-mono mt-2 max-h-[52vh] space-y-1 overflow-auto rounded-md border border-border bg-background/50 p-3 text-[11px] leading-relaxed">
            {log.length === 0 && <div className="text-muted-foreground">idle — waiting for activity…</div>}
            {log.map((l, i) => (
              <div key={i}><span className="text-muted-foreground">{l.t}</span> <span className="text-foreground/90">{l.text}</span></div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  );
}

function nowClock(): string {
  const d = new Date();
  return d.toTimeString().slice(0, 8);
}

// SteamRIP titles look like "Foo Free Download (v1.2)"; trim the boilerplate.
function cleanTitle(t: string): string {
  return t.replace(/\s*Free Download.*$/i, "").trim() || t;
}
