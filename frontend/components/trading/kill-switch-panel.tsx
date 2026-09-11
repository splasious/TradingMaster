"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { apiFetch } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useKillSwitch } from "@/lib/hooks";

export function KillSwitchPanel() {
  const { hasRole } = useAuth();
  const { data: killSwitch } = useKillSwitch();
  const queryClient = useQueryClient();
  const [reason, setReason] = useState("");

  const activateMutation = useMutation({
    mutationFn: () => apiFetch("/api/v1/live-trading/kill-switch/activate", { method: "POST", body: JSON.stringify({ reason }) }),
    onSuccess: () => {
      setReason("");
      queryClient.invalidateQueries({ queryKey: ["kill-switch"] });
      queryClient.invalidateQueries({ queryKey: ["live-deployments"] });
    },
  });
  const deactivateMutation = useMutation({
    mutationFn: () => apiFetch("/api/v1/live-trading/kill-switch/deactivate", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["kill-switch"] }),
  });

  return (
    <Card className={killSwitch?.active ? "border-critical" : undefined}>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <AlertOctagon className="h-4 w-4" /> Global Emergency Stop
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center gap-2">
          <Badge tone={killSwitch?.active ? "critical" : "positive"}>{killSwitch?.active ? "ACTIVE" : "Inactive"}</Badge>
          {killSwitch?.active && killSwitch.reason && <span className="text-sm text-text-secondary">{killSwitch.reason}</span>}
        </div>
        {hasRole("administrator") && (
          <>
            {!killSwitch?.active ? (
              <div className="flex gap-2">
                <Input placeholder="Reason for activating..." value={reason} onChange={(e) => setReason(e.target.value)} />
                <Button variant="destructive" onClick={() => activateMutation.mutate()} disabled={!reason || activateMutation.isPending}>
                  Activate Kill Switch
                </Button>
              </div>
            ) : (
              <Button variant="secondary" onClick={() => deactivateMutation.mutate()} disabled={deactivateMutation.isPending}>
                Deactivate
              </Button>
            )}
          </>
        )}
        <p className="text-xs text-text-muted">Activating immediately stops all active live deployments and blocks new orders until deactivated.</p>
      </CardContent>
    </Card>
  );
}
