"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";
import { useAuth } from "@/lib/auth-context";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { status } = useAuth();
  const router = useRouter();
  // Below the lg breakpoint the sidebar is a drawer opened from the top bar.
  const [navOpen, setNavOpen] = useState(false);
  const openNav = useCallback(() => setNavOpen(true), []);
  const closeNav = useCallback(() => setNavOpen(false), []);

  useEffect(() => {
    if (status === "unauthenticated") router.replace("/login");
  }, [status, router]);

  if (status !== "authenticated") {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-brand" />
      </div>
    );
  }

  return (
    <div className="flex h-dvh w-full">
      <Sidebar mobileOpen={navOpen} onClose={closeNav} />
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <Topbar onOpenNav={openNav} />
        <main className="min-w-0 flex-1 overflow-y-auto bg-background p-3 sm:p-5 lg:p-6">{children}</main>
      </div>
    </div>
  );
}
