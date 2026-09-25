"use client";

import { Bell, CheckCircle2, ChevronDown, LogOut, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/lib/auth-context";
import { useSystemHealth, useUnreadAlertCount } from "@/lib/hooks";
import { cn } from "@/lib/utils";

import { DataFreshnessPill } from "./data-freshness-pill";
import { EnvironmentBadge } from "./environment-badge";
import { ThemeMenuItem, ThemeToggle } from "./theme-toggle";

function AlertsBell() {
  const { data } = useUnreadAlertCount();
  const count = data?.unread_count ?? 0;

  return (
    <Link href="/alerts" className="relative flex h-8 w-8 items-center justify-center rounded-md text-text-secondary hover:bg-surface-elevated hover:text-text-primary">
      <Bell className="h-4 w-4" />
      {count > 0 && (
        <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-critical px-1 text-[10px] font-semibold text-white">
          {count > 99 ? "99+" : count}
        </span>
      )}
    </Link>
  );
}

function SystemHealthIndicator() {
  const { data, isLoading } = useSystemHealth();
  const healthy = data?.status === "healthy";

  return (
    <div
      className={cn(
        "flex shrink-0 items-center gap-1.5 rounded-full px-1.5 py-1.5 text-xs font-medium xl:px-2.5 xl:py-1",
        isLoading ? "bg-inactive-soft text-inactive" : healthy ? "bg-positive-soft text-positive" : "bg-critical-soft text-critical",
      )}
      title={data ? Object.entries(data.components).map(([k, v]) => `${k}: ${v}`).join(", ") : "Checking..."}
    >
      {healthy ? <CheckCircle2 className="h-3.5 w-3.5 shrink-0" /> : <XCircle className="h-3.5 w-3.5 shrink-0" />}
      <span className="hidden xl:inline">{isLoading ? "Checking" : healthy ? "All systems healthy" : "Degraded"}</span>
    </div>
  );
}

export function Topbar() {
  const { user, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-border bg-surface px-3 sm:gap-3 sm:px-5">
      <div className="flex min-w-0 items-center gap-1.5 sm:gap-2">
        <SystemHealthIndicator />
        <DataFreshnessPill />
      </div>

      <div className="flex shrink-0 items-center gap-0.5 sm:gap-3">
        <EnvironmentBadge />
        <AlertsBell />
        {/* On a phone the theme switch is in the account menu instead. */}
        <div className="hidden sm:block">
          <ThemeToggle />
        </div>

        <div className="relative">
          <button
            onClick={() => setMenuOpen((v) => !v)}
            aria-label="Account menu"
            className="flex items-center gap-2 rounded-md px-1.5 py-1.5 text-sm text-text-secondary hover:bg-surface-elevated sm:px-2"
          >
            <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand text-xs font-semibold text-brand-foreground">
              {user?.full_name?.[0]?.toUpperCase() ?? "?"}
            </span>
            <span className="hidden text-text-primary 2xl:inline">{user?.full_name}</span>
            <ChevronDown className="hidden h-3.5 w-3.5 2xl:block" />
          </button>

          {menuOpen && (
            <div className="absolute right-0 z-20 mt-1 w-48 rounded-md border border-border bg-surface-elevated py-1 shadow-lg">
              <div className="border-b border-border px-3 py-2">
                <div className="text-sm font-medium text-text-primary 2xl:hidden">{user?.full_name}</div>
                <div className="truncate text-xs text-text-muted">{user?.email}</div>
                <div className="mt-0.5 text-xs capitalize text-text-secondary">{user?.roles.join(", ")}</div>
              </div>
              <ThemeMenuItem onDone={() => setMenuOpen(false)} className="sm:hidden" />
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
