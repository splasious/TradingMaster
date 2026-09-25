"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { apiFetch } from "@/lib/api";
import type { BfSchedule, BfSource } from "@/lib/types";

type ScheduleIn = Omit<BfSchedule, "next_run_at" | "worker_paused">;

/** Every control on the Data Backfill page -- each refreshes the overview,
 * the "saved up to" status and the stocks table once it's done. */
export function useBackfillActions() {
  const queryClient = useQueryClient();
  const refresh = () => {
    for (const key of ["bf-overview", "bf-freshness", "bf-stocks", "bf-jobs"]) {
      queryClient.invalidateQueries({ queryKey: [key] });
    }
  };

  const topUp = useMutation({
    mutationFn: (sources?: BfSource[]) =>
      apiFetch<{ queued: Record<string, number> }>("/api/v1/backfill-platform/topup", {
        method: "POST",
        body: JSON.stringify({ sources: sources ?? null }),
      }),
    onSuccess: refresh,
  });
  const retryFailed = useMutation({
    mutationFn: (kind: "interrupted" | "failed") =>
      apiFetch<{ requeued: number }>("/api/v1/backfill-platform/jobs/retry-failed", { method: "POST", body: JSON.stringify({ kind }) }),
    onSuccess: refresh,
  });
  const setPaused = useMutation({
    mutationFn: (paused: boolean) =>
      apiFetch<{ paused: boolean }>(`/api/v1/backfill-platform/worker/${paused ? "pause" : "resume"}`, { method: "POST" }),
    onSuccess: refresh,
  });
  const cancelRun = useMutation({
    mutationFn: () => apiFetch<{ runs: number; jobs_cancelled: number }>("/api/v1/backfill-platform/runs/cancel", { method: "POST" }),
    onSuccess: refresh,
  });
  const saveSchedule = useMutation({
    mutationFn: (payload: ScheduleIn) =>
      apiFetch<BfSchedule>("/api/v1/backfill-platform/schedule", { method: "PUT", body: JSON.stringify(payload) }),
    onSuccess: refresh,
  });

  return { topUp, retryFailed, setPaused, cancelRun, saveSchedule };
}
