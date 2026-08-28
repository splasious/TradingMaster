"use client";

import { CheckCircle2, ChevronDown, LogOut, XCircle } from "lucide-react";
import { useState } from "react";

import { useAuth } from "@/lib/auth-context";
import { useSystemHealth } from "@/lib/hooks";
import { cn } from "@/lib/utils";

import { EnvironmentBadge } from "./environment-badge";
import { ThemeToggle } from "./theme-toggle";

function SystemHealthIndicator() {
  const { data, isLoading } = useSystemHealth();
  const healthy = data?.status === "healthy";

  return (
    <div
      className={cn(
        "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium",
        isLoading ? "bg-inactive-soft text-inactive" : healthy ? "bg-positive-soft text-positive" : "bg-critical-soft text-critical",
      )}
      title={data ? Object.entries(data.components).map(([k, v]) => `${k}: ${v}`).join(", ") : "Checking..."}
    >
      {healthy ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
      {isLoading ? "Checking" : healthy ? "All systems healthy" : "Degraded"}
    </div>
  );
}

export function Topbar() {
  const { user, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <header className="flex h-14 items-center justify-between border-b border-border bg-surface px-5">
      <SystemHealthIndicator />

      <div className="flex items-center gap-3">
        <EnvironmentBadge />
        <ThemeToggle />

        <div className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-text-secondary hover:bg-surface-elevated"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand text-xs font-semibold text-brand-foreground">
              {user?.full_name?.[0]?.toUpperCase() ?? "?"}
            </span>
            <span className="text-text-primary">{user?.full_name}</span>
            <ChevronDown className="h-3.5 w-3.5" />
          </button>

          {menuOpen && (
            <div className="absolute right-0 z-20 mt-1 w-48 rounded-md border border-border bg-surface-elevated py-1 shadow-lg">
              <div className="border-b border-border px-3 py-2">
                <div className="text-xs text-text-muted">{user?.email}</div>
                <div className="mt-0.5 text-xs capitalize text-text-secondary">{user?.roles.join(", ")}</div>
              </div>
              <button
                onClick={() => logout()}
                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-negative hover:bg-negative-soft"
              >
                <LogOut className="h-3.5 w-3.5" />
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
