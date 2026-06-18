import { useEffect, useState, type ReactNode } from "react";
import {
  Activity,
  Bot,
  CheckCircle2,
  Clock,
  HeartPulse,
  KanbanSquare,
  RefreshCcw,
  Server,
  Smartphone,
  TerminalSquare,
  Users,
  Zap,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { api } from "@/lib/api";
import type { ControlCenterResponse } from "@/lib/api";

const BOARD_LABELS: Record<string, string> = {
  "jarvis-os": "Jarvis OS",
  "sycode-ai": "Sycode AI",
  "sycode-trading": "Sycode Trading",
  upero: "Upero",
};

function formatTime(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function statusTone(status?: string | null): "success" | "warning" | "destructive" | "secondary" {
  const normalized = (status || "").toLowerCase();
  if (["success", "completed", "done", "ready"].includes(normalized)) return "success";
  if (["running", "scheduled", "pending"].includes(normalized)) return "warning";
  if (["failed", "crashed", "error", "timed_out", "blocked"].includes(normalized)) return "destructive";
  return "secondary";
}

function StatPill({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-border/70 bg-background/40 px-3 py-2">
      <div className="text-[11px] uppercase tracking-[0.16em] text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold text-foreground">{value}</div>
    </div>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return <p className="rounded-xl border border-dashed border-border p-4 text-sm text-muted-foreground">{children}</p>;
}

export default function ControlCenterPage() {
  const [data, setData] = useState<ControlCenterResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getControlCenter());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  if (loading && !data) {
    return (
      <div className="flex min-h-[55vh] items-center justify-center text-muted-foreground">
        <Spinner />
        <span className="ml-3">Loading Mission Control…</span>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 p-4 md:p-6">
      <header className="flex flex-col gap-3 rounded-3xl border border-border/70 bg-gradient-to-br from-background via-background to-muted/30 p-5 shadow-sm md:flex-row md:items-center md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2 text-sm font-medium text-primary">
            <Smartphone className="h-4 w-4" />
            DGX Mission Control · read-only
          </div>
          <H2 className="mb-1">Control Center</H2>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Unified fleet view for kanban boards, cron health, DGX checks, voice escalation, and live agent traces. No controls or secrets are exposed here.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground">Updated {formatTime(data?.generated_at)}</span>
          <Button ghost onClick={load} disabled={loading}>
            {loading ? <Spinner /> : <RefreshCcw className="h-4 w-4" />}
            Refresh
          </Button>
        </div>
      </header>

      {error ? <EmptyState>Control Center refresh failed: {error}</EmptyState> : null}

      <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card className="md:col-span-1">
          <CardContent className="p-5">
            <div className="mb-3 flex items-center gap-2">
              <HeartPulse className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">DGX Health</h3>
            </div>
            <Badge tone={statusTone(data?.dgx_health.last_status)}>{data?.dgx_health.last_status || "unknown"}</Badge>
            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Source</dt><dd>{data?.dgx_health.name || data?.dgx_health.message || "cron"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Last run</dt><dd>{formatTime(data?.dgx_health.last_run_at)}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Next run</dt><dd>{formatTime(data?.dgx_health.next_run_at)}</dd></div>
            </dl>
          </CardContent>
        </Card>
        <Card className="md:col-span-1">
          <CardContent className="p-5">
            <div className="mb-3 flex items-center gap-2">
              <Zap className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Voice Escalation</h3>
            </div>
            <Badge tone={statusTone(data?.voice_escalation.state)}>{data?.voice_escalation.state || "unknown"}</Badge>
            <p className="mt-4 break-words text-sm text-muted-foreground">{data?.voice_escalation.message || data?.voice_escalation.source || "No voice state source found."}</p>
          </CardContent>
        </Card>
        <Card className="md:col-span-1">
          <CardContent className="p-5">
            <div className="mb-3 flex items-center gap-2">
              <Server className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Active Agents</h3>
            </div>
            <div className="text-4xl font-semibold">
              {data?.boards.reduce((total, board) => total + (board.counts.running || 0), 0) ?? 0}
            </div>
            <p className="mt-2 text-sm text-muted-foreground">Running kanban workers across the four core boards.</p>
          </CardContent>
        </Card>
        <Card className="md:col-span-1">
          <CardContent className="p-5">
            <div className="mb-3 flex items-center gap-2">
              <Activity className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Dashboard Status</h3>
            </div>
            <Badge tone={data?.status.gateway_running ? "success" : "warning"}>
              {data?.status.gateway_state || (data?.status.gateway_running ? "running" : "stopped")}
            </Badge>
            <dl className="mt-4 space-y-2 text-sm">
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Version</dt><dd>{data?.status.version || "—"}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Sessions</dt><dd>{data?.status.active_sessions ?? 0}</dd></div>
              <div className="flex justify-between gap-3"><dt className="text-muted-foreground">Auth</dt><dd>{data?.status.auth_required ? "gated" : "loopback"}</dd></div>
            </dl>
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-3">
          <CardContent className="p-5">
            <div className="mb-4 flex items-center gap-2">
              <Users className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Profiles</h3>
            </div>
            <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
              {data?.profiles.slice(0, 12).map((profile) => (
                <div key={profile.name} className="rounded-xl border border-border/70 bg-background/50 p-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={profile.gateway_running ? "success" : "secondary"}>{profile.gateway_running ? "gateway" : "idle"}</Badge>
                    {profile.is_default ? <Badge tone="warning">default</Badge> : null}
                    <span className="font-medium">{profile.name}</span>
                  </div>
                  <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">
                    {profile.description || `${profile.provider || "provider ?"} · ${profile.model || "model ?"}`}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                    <span>{profile.skill_count} skills</span>
                    <span>{profile.has_env ? ".env present" : "no .env"}</span>
                    <span>{profile.has_alias ? "alias" : "no alias"}</span>
                  </div>
                </div>
              ))}
            </div>
            {!data?.profiles.length ? <EmptyState>No profiles found under the local Hermes home.</EmptyState> : null}
          </CardContent>
        </Card>
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        {data?.boards.map((board) => (
          <Card key={board.slug}>
            <CardContent className="p-5">
              <div className="mb-4 flex items-start justify-between gap-3">
                <div className="flex items-center gap-2">
                  <KanbanSquare className="h-5 w-5 text-primary" />
                  <div>
                    <h3 className="font-semibold">{BOARD_LABELS[board.slug] || board.slug}</h3>
                    <p className="text-xs text-muted-foreground">{board.available ? "kanban.db connected" : "board DB missing"}</p>
                  </div>
                </div>
                <Badge tone={board.available ? "success" : "warning"}>{board.slug}</Badge>
              </div>
              <div className="mb-4 grid grid-cols-3 gap-2 sm:grid-cols-5">
                {Object.entries(board.counts).map(([status, count]) => (
                  <StatPill key={status} label={status} value={count} />
                ))}
                {Object.keys(board.counts).length === 0 ? <EmptyState>No task counts yet.</EmptyState> : null}
              </div>
              <div className="space-y-2">
                {board.in_flight.map((task) => (
                  <div key={task.id} className="rounded-xl border border-border/70 bg-background/50 p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone={statusTone(task.status)}>{task.status}</Badge>
                      <span className="font-medium">{task.title}</span>
                    </div>
                    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                      <span>{task.id}</span>
                      <span>assignee {task.assignee || "—"}</span>
                      <span>heartbeat {formatTime(task.last_heartbeat_at)}</span>
                    </div>
                  </div>
                ))}
                {board.in_flight.length === 0 ? <EmptyState>No running, ready, or blocked tasks.</EmptyState> : null}
              </div>
            </CardContent>
          </Card>
        ))}
      </section>

      <section className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardContent className="p-5">
            <div className="mb-4 flex items-center gap-2">
              <Clock className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Cron Jobs</h3>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead className="text-xs uppercase tracking-[0.16em] text-muted-foreground">
                  <tr><th className="py-2">Job</th><th>Status</th><th>Last run</th><th>Next run</th></tr>
                </thead>
                <tbody>
                  {data?.cron_jobs.map((job) => (
                    <tr key={`${job.profile}:${job.id}`} className="border-t border-border/70">
                      <td className="py-3"><div className="font-medium">{job.name}</div><div className="text-xs text-muted-foreground">{job.profile} · {job.schedule}</div></td>
                      <td><Badge tone={statusTone(job.last_status)}>{job.last_status || (job.enabled ? "scheduled" : "paused")}</Badge></td>
                      <td>{formatTime(job.last_run_at)}</td>
                      <td>{formatTime(job.next_run_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {!data?.cron_jobs.length ? <EmptyState>No cron jobs visible.</EmptyState> : null}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-5">
            <div className="mb-4 flex items-center gap-2">
              <TerminalSquare className="h-5 w-5 text-primary" />
              <h3 className="font-semibold">Live Agent Traces</h3>
            </div>
            <div className="space-y-3">
              {data?.live_traces.map((trace) => (
                <div key={trace.path} className="rounded-xl border border-border/70 bg-background/50 p-3">
                  <div className="mb-2 flex items-center gap-2 text-xs text-muted-foreground"><Bot className="h-3.5 w-3.5" />{trace.path}</div>
                  <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs leading-relaxed text-muted-foreground">
                    {trace.lines.map((line) => `${line.role || "trace"}: ${line.content}`).join("\n")}
                  </pre>
                </div>
              ))}
              {!data?.live_traces.length ? <EmptyState>No session JSONL traces found.</EmptyState> : null}
            </div>
          </CardContent>
        </Card>
      </section>

      <footer className="flex items-center gap-2 text-xs text-muted-foreground">
        <CheckCircle2 className="h-4 w-4" /> Read-only dashboard: no mutation endpoints are called by this page.
        <Activity className="ml-2 h-4 w-4" /> Auto-refreshes every 30s.
      </footer>
    </div>
  );
}
