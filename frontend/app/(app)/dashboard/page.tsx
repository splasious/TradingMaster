"use client";

import { CheckCircle2, XCircle } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAuth } from "@/lib/auth-context";
import { useSystemHealth } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const COMPONENT_LABELS: Record<string, string> = {
  database: "Database",
  broker_engine: "Broker Engine",
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
            const healthy = value === "healthy";
            return (
              <div key={key} className="flex items-center justify-between text-sm">
                <span className="text-text-secondary">{COMPONENT_LABELS[key] ?? key}</span>
                <span className={cn("flex items-center gap-1.5 font-medium", healthy ? "text-positive" : "text-critical")}>
                  {healthy ? <CheckCircle2 className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
                  {healthy ? "Healthy" : "Error"}
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
        <PlaceholderPanel title="Market Overview" phase={2} />
        <PlaceholderPanel title="Strategy Summary" phase={4} />
      </div>
    </div>
  );
}
