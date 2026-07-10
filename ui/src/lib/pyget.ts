// Client for the local PyGet bridge (api_server.py). REST for commands, SSE for
// live progress. The backend runs on 127.0.0.1 and sends permissive CORS, so
// the same absolute base works in the Vite dev server and in the Electron build.

const API = (typeof window !== "undefined" && (window as any).PYGET_API)
  || "http://127.0.0.1:8787";

export type Task = {
  id: number;
  name: string;
  url: string;
  size: number;
  done: number;
  speed: number;
  status: string;
  progress: number;
  dest_dir: string;
  stream: boolean;
  source: string;
};

export type SearchResult = { title: string; url: string; source: string };

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const pyget = {
  health: () => j<{ ok: boolean; stream_extract: boolean }>("/api/health"),

  search: (q: string, site = "steamrip", page = 1) =>
    j<{ results: SearchResult[] }>(
      `/api/search?q=${encodeURIComponent(q)}&site=${site}&page=${page}`,
    ).then((r) => r.results),

  tasks: () => j<{ tasks: Task[] }>("/api/tasks").then((r) => r.tasks),

  add: (url: string, title?: string) =>
    j("/api/add", { method: "POST", body: JSON.stringify({ url, title }) }),

  start: () => j("/api/start", { method: "POST" }),
  stop: () => j("/api/stop", { method: "POST" }),
  pause: (id: number) => j(`/api/tasks/${id}/pause`, { method: "POST" }),
  resume: (id: number) => j(`/api/tasks/${id}/resume`, { method: "POST" }),
  cancel: (id: number) => j(`/api/tasks/${id}/cancel`, { method: "POST" }),

  getSettings: () => j<Record<string, unknown>>("/api/settings"),
  setSettings: (s: Record<string, unknown>) =>
    j("/api/settings", { method: "POST", body: JSON.stringify(s) }),

  // Server-Sent Events stream. Returns an unsubscribe fn.
  events(handlers: {
    onTask?: (t: Task) => void;
    onRemoved?: (id: number) => void;
    onLog?: (text: string) => void;
    onSnapshot?: (tasks: Task[]) => void;
    onManual?: (urls: string[], text: string) => void;
  }): () => void {
    const es = new EventSource(`${API}/api/events`);
    es.onmessage = (e) => {
      let msg: any;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      switch (msg.type) {
        case "snapshot": handlers.onSnapshot?.(msg.tasks); break;
        case "task": handlers.onTask?.(msg.task); break;
        case "removed": handlers.onRemoved?.(msg.id); break;
        case "log": handlers.onLog?.(msg.text); break;
        case "manual": handlers.onManual?.(msg.urls, msg.text); break;
      }
    };
    return () => es.close();
  },
};

export function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtSpeed(bps: number): string {
  return bps ? `${fmtBytes(bps)}/s` : "—";
}

export function fmtEta(total: number, done: number, speed: number): string {
  if (!speed || !total || done >= total) return "—";
  const s = Math.round((total - done) / speed);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${m}m` : m ? `${m}m ${sec}s` : `${sec}s`;
}
