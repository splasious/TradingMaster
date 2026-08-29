"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon, AlertTriangle, CheckCheck, Info } from "lucide-react";
import { useState } from "react";

import { Badge, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { apiFetch } from "@/lib/api";
import { useAlerts } from "@/lib/hooks";
import type { AlertOut, AlertSeverity } from "@/lib/types";

const SEVERITY_CONFIG: Record<AlertSeverity, { label: string; tone: Tone; Icon: typeof Info }> = {
  info: { label: "Info", tone: "neutral", Icon: Info },
  warning: { label: "Warning", tone: "warning", Icon: AlertTriangle },
  critical: { label: "Critical", tone: "critical", Icon: AlertOctagon },
};

function AlertRow({ alert }: { alert: AlertOut }) {
  const queryClient = useQueryClient();
  const { label, tone, Icon } = SEVERITY_CONFIG[alert.severity];

  const markReadMutation = useMutation({
    mutationFn: () => apiFetch(`/api/v1/alerts/${alert.id}/read`, { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alerts-unread-count"] });
    },
  });

  return (
    <div className={`flex items-start gap-3 border-b border-border p-4 last:border-0 ${alert.is_read ? "opacity-60" : ""}`}>
      <Badge tone={tone} className="mt-0.5 shrink-0">
        <Icon className="h-3 w-3" />
        {label}
      </Badge>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm font-medium text-text-primary">{alert.title}</p>
          <span className="shrink-0 text-xs text-text-muted">{new Date(alert.created_at).toLocaleString()}</span>
        </div>
        <p className="mt-0.5 text-sm text-text-secondary">{alert.message}</p>
      </div>
      {!alert.is_read && (
        <Button variant="ghost" size="sm" onClick={() => markReadMutation.mutate()} disabled={markReadMutation.isPending}>
          Mark read
        </Button>
      )}
    </div>
  );
}

export default function AlertsPage() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [severity, setSeverity] = useState<AlertSeverity | "">("");
  const { data: alerts, isLoading } = useAlerts(unreadOnly, severity || null);
  const queryClient = useQueryClient();

  const markAllReadMutation = useMutation({
    mutationFn: () => apiFetch("/api/v1/alerts/read-all", { method: "POST" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["alerts"] });
      queryClient.invalidateQueries({ queryKey: ["alerts-unread-count"] });
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Alerts</h1>
          <p className="text-sm text-text-muted">Order fills, rejections, stop/target triggers, risk breaches, and system events.</p>
        </div>
        <Button variant="secondary" onClick={() => markAllReadMutation.mutate()} disabled={markAllReadMutation.isPending}>
          <CheckCheck className="h-3.5 w-3.5" /> Mark all read
        </Button>
      </div>

      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm text-text-secondary">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} className="h-4 w-4 rounded border-border" />
          Unread only
        </label>
        <Select className="w-40" value={severity} onChange={(e) => setSeverity(e.target.value as AlertSeverity | "")}>
          <option value="">All severities</option>
          <option value="info">Info</option>
          <option value="warning">Warning</option>
          <option value="critical">Critical</option>
        </Select>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent Alerts</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <p className="p-5 text-sm text-text-muted">Loading...</p>
          ) : !alerts?.length ? (
            <p className="p-5 text-sm text-text-muted">No alerts to show.</p>
          ) : (
            alerts.map((a) => <AlertRow key={a.id} alert={a} />)
          )}
        </CardContent>
      </Card>
    </div>
  );
}
