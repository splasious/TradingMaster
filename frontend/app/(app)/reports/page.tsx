"use client";

import { useMutation } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { apiDownload } from "@/lib/api";
import { useReportSummary } from "@/lib/hooks";

export default function ReportsPage() {
  const [environment, setEnvironment] = useState<"" | "paper" | "live">("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const { data: summary, isLoading } = useReportSummary(environment || null, start || null, end || null);

  const downloadMutation = useMutation({
    mutationFn: () => {
      const params = new URLSearchParams();
      if (environment) params.set("environment", environment);
      if (start) params.set("start", start);
      if (end) params.set("end", end);
      return apiDownload(`/api/v1/reports/trades.csv?${params.toString()}`, "trades.csv");
    },
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Reports</h1>
          <p className="text-sm text-text-muted">Trade history and summary statistics across paper and live trading.</p>
        </div>
        <Button onClick={() => downloadMutation.mutate()} disabled={downloadMutation.isPending}>
          <Download className="h-3.5 w-3.5" /> {downloadMutation.isPending ? "Preparing..." : "Download CSV"}
        </Button>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-secondary">Environment</label>
          <Select className="w-36" value={environment} onChange={(e) => setEnvironment(e.target.value as "" | "paper" | "live")}>
            <option value="">All</option>
            <option value="paper">Paper</option>
            <option value="live">Live</option>
          </Select>
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-secondary">From</label>
          <Input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
        </div>
        <div className="space-y-1.5">
          <label className="text-xs font-medium text-text-secondary">To</label>
          <Input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </div>
      </div>

      {downloadMutation.isError && (
        <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">Failed to download report.</div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Summary</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-2 gap-4 pt-4 md:grid-cols-5">
          {isLoading || !summary ? (
            <p className="text-sm text-text-muted">Loading...</p>
          ) : (
            <>
              <div>
                <div className="text-xs text-text-muted">Trades</div>
                <div className="font-financial text-xl font-semibold text-text-primary">{summary.trade_count}</div>
              </div>
              <div>
                <div className="text-xs text-text-muted">Net P&amp;L</div>
                <div className={`font-financial text-xl font-semibold ${summary.net_pnl >= 0 ? "text-positive" : "text-negative"}`}>
                  {summary.net_pnl.toFixed(2)}
                </div>
              </div>
              <div>
                <div className="text-xs text-text-muted">Win rate</div>
                <div className="font-financial text-xl font-semibold text-text-primary">{summary.win_rate_pct.toFixed(1)}%</div>
              </div>
              <div>
                <div className="text-xs text-text-muted">Best trade</div>
                <div className="font-financial text-xl font-semibold text-positive">{summary.best_trade.toFixed(2)}</div>
              </div>
              <div>
                <div className="text-xs text-text-muted">Worst trade</div>
                <div className="font-financial text-xl font-semibold text-negative">{summary.worst_trade.toFixed(2)}</div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
