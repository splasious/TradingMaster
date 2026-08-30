"use client";

import { Radio, TriangleAlert } from "lucide-react";

import { useBfLiveSyncStatus } from "@/lib/hooks";

export function LiveSyncStatus() {
  const { data: status } = useBfLiveSyncStatus();

  if (!status) return null;

  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-surface-elevated px-3 py-2 text-xs">
      {status.running ? (
        <Radio className="h-3.5 w-3.5 shrink-0 text-positive" />
      ) : (
        <TriangleAlert className="h-3.5 w-3.5 shrink-0 text-warning" />
      )}
      <span className="text-text-secondary">
        Live sync engine: <span className={status.running ? "text-positive" : "text-warning"}>{status.running ? "running" : "stopped"}</span>
        {status.last_sync_at && (
          <>
            {" "}-- last polled {status.last_synced_count} symbol{status.last_synced_count === 1 ? "" : "s"} at{" "}
            {new Date(status.last_sync_at).toLocaleTimeString()}
          </>
        )}
        {status.last_error && <span className="text-negative"> -- last error: {status.last_error}</span>}
      </span>
      <span className="ml-auto text-text-muted">
        Delta polled every 60s always; NSE/Yahoo polled only during market hours (09:15-15:30 IST, weekdays).
      </span>
    </div>
  );
}
