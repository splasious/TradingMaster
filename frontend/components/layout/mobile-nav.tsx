"use client";

import { LayoutGrid, Menu, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

import { BOTTOM_TABS, isActivePath, NAV_SECTIONS } from "@/lib/nav";
import { cn } from "@/lib/utils";

const ICONS = new Map(NAV_SECTIONS.flatMap((s) => s.items).map((i) => [i.href, i.icon]));
const TAB_HREFS = new Set(BOTTOM_TABS.map((t) => t.href));
const MORE_SECTIONS = NAV_SECTIONS.map((s) => ({ ...s, items: s.items.filter((i) => !TAB_HREFS.has(i.href)) })).filter((s) => s.items.length);

function TabButton({ active, label, children }: { active: boolean; label: string; children: React.ReactNode }) {
  return (
    <span className={cn("flex flex-col items-center gap-0.5 text-[11px]", active ? "font-semibold text-active" : "font-medium text-text-muted")}>
      <span className={cn("flex h-7 w-12 items-center justify-center rounded-full transition-colors", active && "bg-active-soft")}>{children}</span>
      {label}
    </span>
  );
}

/** Phones and tablets (below lg): a bottom bar with the most-used pages and
 * a "More" sheet with every other page, grouped like the desktop sidebar. */
export function MobileNav() {
  const pathname = usePathname();
  const [moreOpen, setMoreOpen] = useState(false);
  const onMorePage = !BOTTOM_TABS.some((t) => isActivePath(pathname, t.href));

  useEffect(() => {
    if (!moreOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMoreOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [moreOpen]);

  return (
    <>
      <div
        onClick={() => setMoreOpen(false)}
        aria-hidden
        className={cn(
          "fixed inset-0 z-40 bg-black/50 transition-opacity duration-200 lg:hidden",
          moreOpen ? "opacity-100" : "pointer-events-none opacity-0",
        )}
      />
      <div
        role="dialog"
        aria-label="All pages"
        aria-hidden={!moreOpen}
        className={cn(
          "fixed inset-x-0 bottom-[calc(3.75rem+env(safe-area-inset-bottom))] z-40 max-h-[75dvh] overflow-y-auto rounded-t-2xl border-t border-border bg-surface px-3.5 pb-3 pt-2 transition-[translate,visibility] duration-200 lg:hidden",
          moreOpen ? "translate-y-0" : "invisible translate-y-full",
        )}
      >
        <div className="mx-auto mb-2 h-1 w-9 rounded-full bg-border" />
        <div className="flex items-center justify-between px-1">
          <h2 className="text-[15px] font-semibold text-text-primary">All pages</h2>
          <button
            onClick={() => setMoreOpen(false)}
            aria-label="Close"
            className="-mr-1 flex h-9 w-9 items-center justify-center rounded-md text-text-secondary hover:bg-surface-elevated"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        {MORE_SECTIONS.map((section) => (
          <div key={section.label}>
            <div className="mx-1 mb-1.5 mt-2.5 text-[10.5px] font-semibold uppercase tracking-wider text-text-muted">{section.label}</div>
            <div className="grid grid-cols-4 gap-1.5 sm:grid-cols-6">
              {section.items.map((item) => {
                const Icon = item.icon;
                const active = isActivePath(pathname, item.href);
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    onClick={() => setMoreOpen(false)}
                    className={cn(
                      "flex flex-col items-center gap-1.5 rounded-lg border px-0.5 py-2.5 text-center text-[11px] leading-tight",
                      active ? "border-active/40 bg-active-soft text-active" : "border-border bg-surface-elevated text-text-secondary",
                    )}
                  >
                    <Icon className="h-5 w-5" aria-hidden />
                    {item.label}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      <nav
        aria-label="Main navigation"
        className="relative z-50 grid shrink-0 grid-cols-5 border-t border-border bg-surface pb-[env(safe-area-inset-bottom)] lg:hidden"
      >
        {BOTTOM_TABS.map((tab) => {
          const Icon = ICONS.get(tab.href) ?? LayoutGrid;
          const active = !moreOpen && isActivePath(pathname, tab.href);
          return (
            <Link key={tab.href} href={tab.href} onClick={() => setMoreOpen(false)} aria-current={active ? "page" : undefined} className="flex h-15 items-center justify-center">
              <TabButton active={active} label={tab.label}>
                <Icon className="h-5 w-5" aria-hidden />
              </TabButton>
            </Link>
          );
        })}
        <button onClick={() => setMoreOpen((v) => !v)} aria-expanded={moreOpen} className="flex h-15 items-center justify-center">
          <TabButton active={moreOpen || onMorePage} label="More">
            <Menu className="h-5 w-5" aria-hidden />
          </TabButton>
        </button>
      </nav>
    </>
  );
}
