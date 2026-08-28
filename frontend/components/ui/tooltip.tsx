"use client";

import { useState } from "react";

import { cn } from "@/lib/utils";

export function Tooltip({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="relative inline-flex"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {children}
      {open && (
        <span
          role="tooltip"
          className={cn(
            "pointer-events-none absolute bottom-full left-1/2 z-50 mb-2 -translate-x-1/2 whitespace-nowrap rounded-md bg-text-primary px-2 py-1 text-xs text-background",
            className,
          )}
        >
          {label}
        </span>
      )}
    </span>
  );
}
