"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth-context";

export default function RootPage() {
  const { status } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
    if (status === "unauthenticated") router.replace("/login");
  }, [status, router]);

  return (
    <div className="flex h-screen w-full items-center justify-center bg-background">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-brand" />
    </div>
  );
}
