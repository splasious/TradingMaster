"use client";

import { CheckCircle2, CircleDashed, XCircle } from "lucide-react";
import Link from "next/link";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth-context";
import { useInstruments, useSystemHealth } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const COMPONENT_LABELS: Record<string, string> = {
  database: "Database",
  broker_engine: "Broker Engine",
  market_data_yahoo_nse: "Market Data: NSE (Yahoo)",
  market_data_delta: "Market Data: Delta Exchange",
};

function SystemHealthPanel() {
  const { data, isLoading } = useSystemHealth();

  return (
    <Card>
      <CardHeader>
        <CardTitle>System Health</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {isLoading && <p className="text-sm text-text-muted">Checking system components...</p>}
        {data &&
          Object.entries(data.components).map(([key, value]) => {
            const style =
              value === "healthy"
                ? { icon: CheckCircle2, className: "text-positive", label: "Healthy" }
                : value === "unreachable"
                  ? { icon: CircleDashed, className: "text-inactive", label: "Not running" }
                  : { icon: XCircle, className: "text-critical", label: "Error" };
            const Icon = style.icon;
            return (
              <div key={key} className="flex items-center justify-between text-sm">
                <span className="text-text-secondary">{COMPONENT_LABELS[key] ?? key}</span>
                <span className={cn("flex items-center gap-1.5 font-medium", style.className)}>
                  <Icon className="h-3.5 w-3.5" />
                  {style.label}
                </span>
              </div>
            );
          })}
      </CardContent>
    </Card>
  );
}

function PlaceholderPanel({ title, phase }: { title: string; phase: number }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-text-muted">Arrives in Phase {phase}.</p>
      </CardContent>
    </Card>
  );
}

function MarketOverviewPanel() {
  const { data: instruments, isLoading } = useInstruments("");
  const nse = instruments?.filter((i) => i.exchange === "NSE").length ?? 0;
  const delta = instruments?.filter((i) => i.exchange === "DELTA").length ?? 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Market Overview</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2">
        {isLoading ? (
          <p className="text-sm text-text-muted">Loading...</p>
        ) : (
          <>
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">NSE instruments</span>
              <span className="font-financial font-medium text-text-primary">{nse}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-text-secondary">Delta Exchange instruments</span>
              <span className="font-financial font-medium text-text-primary">{delta}</span>
            </div>
            <Link href="/markets" className="mt-1 inline-block text-xs text-active hover:underline">
              View live prices &rarr;
            </Link>
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function DashboardPage() {
  const { user } = useAuth();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Welcome, {user?.full_name}</h1>
        <p className="text-sm text-text-muted">Here&apos;s the current state of the platform.</p>
      </div>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
        <SystemHealthPanel />
        <PlaceholderPanel title="Portfolio" phase={7} />
        <MarketOverviewPanel />
        <PlaceholderPanel title="Strategy Summary" phase={4} />
      </div>
    </div>
  );
}
