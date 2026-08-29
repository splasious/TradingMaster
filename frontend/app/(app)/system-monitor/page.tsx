"use client";

import { CheckCircle2, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useSystemMonitor } from "@/lib/hooks";

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardContent className="pt-4">
        <div className="text-xs text-text-muted">{label}</div>
        <div className="font-financial text-xl font-semibold text-text-primary">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-text-muted">{sub}</div>}
      </CardContent>
    </Card>
  );
}

function RunningBadge({ running, label }: { running: boolean; label: string }) {
  return (
    <Badge tone={running ? "positive" : "inactive"}>
      {running ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {label}: {running ? "Running" : "Stopped"}
    </Badge>
  );
}

export default function SystemMonitorPage() {
  const { data, isLoading } = useSystemMonitor();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">System Monitor</h1>
        <p className="text-sm text-text-muted">Live infrastructure, application, and trading-activity metrics. Refreshes every 5s.</p>
      </div>

      {isLoading || !data ? (
        <p className="text-sm text-text-muted">Loading...</p>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Infrastructure</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-4 pt-4 md:grid-cols-3">
              <MetricCard label="CPU" value={`${data.infrastructure.cpu_percent.toFixed(1)}%`} />
              <MetricCard
                label="Memory"
                value={`${data.infrastructure.memory_percent.toFixed(1)}%`}
                sub={`${data.infrastructure.memory_used_mb.toFixed(0)} / ${data.infrastructure.memory_total_mb.toFixed(0)} MB`}
              />
              <MetricCard
                label="Disk"
                value={`${data.infrastructure.disk_percent.toFixed(1)}%`}
                sub={`${data.infrastructure.disk_used_gb.toFixed(1)} / ${data.infrastructure.disk_total_gb.toFixed(1)} GB`}
              />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Application</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 pt-4">
              <div className="text-sm text-text-secondary">
                Uptime: <span className="font-financial text-text-primary">{Math.floor(data.application.uptime_seconds / 60)} min</span>
              </div>
              <div className="flex flex-wrap gap-2">
                <RunningBadge running={data.application.tick_engine_running} label="Tick engine" />
                <RunningBadge running={data.application.paper_trading_scheduler_running} label="Paper trading scheduler" />
              </div>
              <div className="text-sm text-text-secondary">
                Subscribed instruments:{" "}
                <span className="font-financial text-text-primary">{data.application.tick_engine_subscribed_instruments}</span>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Trading Activity</CardTitle>
            </CardHeader>
            <CardContent className="grid grid-cols-2 gap-4 pt-4">
              <MetricCard label="Active paper deployments" value={String(data.trading.active_paper_deployments)} />
              <MetricCard label="Active live deployments" value={String(data.trading.active_live_deployments)} />
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
