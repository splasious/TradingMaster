"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, RotateCcw, XCircle } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, LoadingState } from "@/components/ui/data-state";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiFetch } from "@/lib/api";
import { useBfJobs } from "@/lib/hooks";
import type { BfBackfillJobOut, BfSource } from "@/lib/types";

const STATUS_ICON: Record<BfBackfillJobOut["status"], React.ReactNode> = {
  completed: <CheckCircle2 className="h-3.5 w-3.5 text-positive" />,
  failed: <XCircle className="h-3.5 w-3.5 text-negative" />,
  running: <Loader2 className="h-3.5 w-3.5 animate-spin text-active" />,
  pending: <Loader2 className="h-3.5 w-3.5 animate-spin text-text-muted" />,
};

const SOURCE_TABS: { value: string; label: string; source?: BfSource }[] = [
  { value: "all", label: "All Sources" },
  { value: "yahoo", label: "Yahoo Finance", source: "yahoo" },
  { value: "delta", label: "Delta Exchange", source: "delta" },
  { value: "zerodha", label: "Zerodha Kite", source: "zerodha" },
];

function JobHistoryTable({ source }: { source?: BfSource }) {
  const queryClient = useQueryClient();
  const { data: jobs, isLoading } = useBfJobs(source);

  const retryMutation = useMutation({
    mutationFn: (jobId: string) => apiFetch(`/api/v1/backfill-platform/jobs/${jobId}/retry`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["bf-jobs"] }),
  });

  if (isLoading) return <LoadingState />;
  if (!jobs?.length) return <EmptyState title="No backfill jobs yet" />;

  return (
    <Table>
      <Thead>
        <tr><Th>Source</Th><Th>Symbol</Th><Th>Timeframe</Th><Th>Status</Th><Th>Result</Th><Th>Started</Th><Th /></tr>
      </Thead>
      <Tbody>
        {jobs.map((job) => (
          <tr key={job.id}>
            <Td className="capitalize">{job.source}</Td>
            <Td className="font-medium">{job.symbol}</Td>
            <Td>{job.timeframe}</Td>
            <Td>
              <span className="flex items-center gap-1.5">
                {STATUS_ICON[job.status]}
                <Badge tone={job.status === "completed" ? "positive" : job.status === "failed" ? "critical" : "active"}>{job.status}</Badge>
              </span>
            </Td>
            <Td className="text-xs text-text-muted">
              {job.status === "completed" ? `${job.inserted_count} new, ${job.duplicate_count} dup` : job.status === "failed" ? job.error_message : "--"}
            </Td>
            <Td className="text-xs text-text-muted">{job.started_at ? new Date(job.started_at).toLocaleString() : "--"}</Td>
            <Td className="text-right">
              {job.status === "failed" && (
                <Button variant="ghost" size="sm" onClick={() => retryMutation.mutate(job.id)} disabled={retryMutation.isPending}>
                  <RotateCcw className="h-3.5 w-3.5" /> Retry
                </Button>
              )}
            </Td>
          </tr>
        ))}
      </Tbody>
    </Table>
  );
}

export function JobHistoryPanel() {
  return (
    <Card>
      <CardHeader><CardTitle>Job History</CardTitle></CardHeader>
      <CardContent className="p-0">
        <Tabs defaultValue="all" className="px-4">
          <TabsList>
            {SOURCE_TABS.map((t) => (
              <TabsTrigger key={t.value} value={t.value}>{t.label}</TabsTrigger>
            ))}
          </TabsList>
          {SOURCE_TABS.map((t) => (
            <TabsContent key={t.value} value={t.value}>
              <JobHistoryTable source={t.source} />
            </TabsContent>
          ))}
        </Tabs>
      </CardContent>
    </Card>
  );
}
