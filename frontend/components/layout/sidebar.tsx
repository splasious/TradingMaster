"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import { NAV_SECTIONS } from "@/lib/nav";

const DEFAULT_WIDTH = 240;
const MIN_WIDTH = 180;
const MAX_WIDTH = 420;
const STORAGE_KEY = "tm_sidebar_width";

function readStoredWidth(): number {
  if (typeof window === "undefined") return DEFAULT_WIDTH;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    const parsed = stored ? Number(stored) : NaN;
    return Number.isFinite(parsed) ? Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, parsed)) : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
}

export function Sidebar() {
  const pathname = usePathname();
  // Sidebar only ever renders after the (app) layout's auth check settles
  // client-side (it shows a loading spinner until then), so there's no
  // server-rendered markup to mismatch here -- safe to read localStorage
  // directly in the initializer rather than syncing it in via an effect.
  const [width, setWidth] = useState(readStoredWidth);
  const [resizing, setResizing] = useState(false);
  const asideRef = useRef<HTMLElement>(null);

  const handlePointerMove = useCallback((e: PointerEvent) => {
    if (!asideRef.current) return;
    const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, e.clientX - asideRef.current.getBoundingClientRect().left));
    setWidth(next);
  }, []);

  const stopResizing = useCallback(() => {
    setResizing(false);
    setWidth((current) => {
      try {
        localStorage.setItem(STORAGE_KEY, String(current));
      } catch {
        // ignore -- per-viewer convenience only, safe to lose
      }
      return current;
    });
  }, []);

  useEffect(() => {
    if (!resizing) return;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", stopResizing);
    return () => {
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", stopResizing);
    };
  }, [resizing, handlePointerMove, stopResizing]);

  return (
    <aside
      ref={asideRef}
      style={{ width }}
      className="relative flex h-screen shrink-0 flex-col border-r border-border bg-surface"
    >
      <div className="flex h-14 items-center gap-2 border-b border-border px-5">
        <div className="h-2.5 w-2.5 shrink-0 rounded-full bg-brand" />
        <span className="truncate text-sm font-semibold tracking-tight text-text-primary">TradingMaster</span>
      </div>

      <nav className="flex-1 overflow-y-auto px-3 py-4">
        {NAV_SECTIONS.map((section) => (
          <div key={section.label} className="mb-5">
            <div className="mb-1.5 px-2 text-[11px] font-semibold uppercase tracking-wider text-text-muted">
              {section.label}
            </div>
            <div className="space-y-0.5">
              {section.items.map((item) => {
                const active = pathname === item.href || pathname.startsWith(item.href + "/");
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      "flex items-center justify-between rounded-md px-2 py-1.5 text-sm transition-colors",
                      active
                        ? "bg-active-soft text-active font-medium"
                        : "text-text-secondary hover:bg-surface-elevated hover:text-text-primary",
                    )}
                  >
                    <span className="flex min-w-0 items-center gap-2.5">
                      <Icon className="h-4 w-4 shrink-0" />
                      <span className="truncate">{item.label}</span>
                    </span>
                    {item.unbuilt && <span className="shrink-0 text-[10px] uppercase tracking-wide text-text-muted">Soon</span>}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Drag handle: a thin invisible hit-area over the border, widened on
          hover/drag so it's actually grabbable without looking heavy at
          rest. Double-click resets to the default width. */}
      <div
        onPointerDown={(e) => {
          e.preventDefault();
          setResizing(true);
        }}
        onDoubleClick={() => {
          setWidth(DEFAULT_WIDTH);
          try {
            localStorage.setItem(STORAGE_KEY, String(DEFAULT_WIDTH));
          } catch {
            // ignore
          }
        }}
        className={cn(
          "absolute inset-y-0 -right-1 z-10 w-2 cursor-col-resize",
          "after:absolute after:inset-y-0 after:left-1/2 after:w-px after:-translate-x-1/2 after:bg-transparent hover:after:bg-active",
          resizing && "after:bg-active",
        )}
        title="Drag to resize, double-click to reset"
      />
    </aside>
  );
}
