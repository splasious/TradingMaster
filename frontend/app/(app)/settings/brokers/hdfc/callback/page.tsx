"use client";

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, XCircle } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/data-state";
import { apiFetch, ApiError } from "@/lib/api";
import type { BrokerAccountOut } from "@/lib/types";

const PENDING_ACCOUNT_KEY = "tm_hdfc_pending_account_id";

export default function HDFCCallbackPage() {
  const searchParams = useSearchParams();
  // HDFC's exact redirect query param name is not confirmed (see
  // hdfc_securities_broker.py's module docstring) -- read every plausible
  // candidate rather than guessing one and silently failing on the others.
  const authCode = searchParams.get("auth_code") || searchParams.get("code") || searchParams.get("authCode");

  const [accountId] = useState(() => (typeof window !== "undefined" ? localStorage.getItem(PENDING_ACCOUNT_KEY) : null));

  const completeMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch<BrokerAccountOut>(`/api/v1/brokers/accounts/${id}/hdfc/callback`, {
        method: "POST",
        body: JSON.stringify({ auth_code: authCode }),
      }),
    onSettled: () => localStorage.removeItem(PENDING_ACCOUNT_KEY),
  });

  useEffect(() => {
    if (!accountId || !authCode) return;
    completeMutation.mutate(accountId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, authCode]);

  const connected = completeMutation.data?.connection_status === "connected";

  return (
    <div className="mx-auto max-w-md space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>HDFC Securities Login</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 pt-4">
          {!authCode ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">HDFC Securities login did not complete</p>
              <p className="text-sm text-text-muted">No authorization code was returned. You can close this tab and try again.</p>
            </div>
          ) : !accountId ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">No pending connection found</p>
              <p className="text-sm text-text-muted">Start the connection again from Settings &gt; Brokers.</p>
            </div>
          ) : completeMutation.isPending || completeMutation.isIdle ? (
            <LoadingState title="Completing connection..." />
          ) : completeMutation.isError ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">Connection failed</p>
              <p className="text-sm text-text-muted">
                {completeMutation.error instanceof ApiError ? completeMutation.error.message : "Something went wrong"}
              </p>
            </div>
          ) : connected ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <CheckCircle2 className="h-6 w-6 text-positive" />
              <p className="text-sm font-medium text-text-primary">HDFC Securities connected</p>
              <p className="text-sm text-text-muted">You can close this tab and return to Settings &gt; Brokers.</p>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">Connection failed</p>
              <p className="text-sm text-text-muted">HDFC Securities rejected the login. Close this tab and try again from Settings &gt; Brokers.</p>
            </div>
          )}
          <Link href="/settings/brokers" className="block text-center text-sm text-active hover:underline">
            Back to Broker Connections
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
