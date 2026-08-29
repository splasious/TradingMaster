"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";
import { NAV_SECTIONS } from "@/lib/nav";

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex h-14 items-center gap-2 border-b border-border px-5">
        <div className="h-2.5 w-2.5 rounded-full bg-brand" />
        <span className="text-sm font-semibold tracking-tight text-text-primary">TradingMaster</span>
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
                    <span className="flex items-center gap-2.5">
                      <Icon className="h-4 w-4" />
                      {item.label}
                    </span>
                    {item.unbuilt && <span className="text-[10px] uppercase tracking-wide text-text-muted">Soon</span>}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>
    </aside>
  );
}
