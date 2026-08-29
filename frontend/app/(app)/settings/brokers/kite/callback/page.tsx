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

const PENDING_ACCOUNT_KEY = "tm_kite_pending_account_id";

export default function KiteCallbackPage() {
  const searchParams = useSearchParams();
  const requestToken = searchParams.get("request_token");
  const loginStatus = searchParams.get("status");

  // Captured once on mount, not re-read from localStorage on every render --
  // the completion effect clears the key once it's done with it, and a
  // live re-read would otherwise make the account id "disappear" mid-flow.
  const [accountId] = useState(() => (typeof window !== "undefined" ? localStorage.getItem(PENDING_ACCOUNT_KEY) : null));

  const completeMutation = useMutation({
    mutationFn: (id: string) =>
      apiFetch<BrokerAccountOut>(`/api/v1/brokers/accounts/${id}/kite/callback`, {
        method: "POST",
        body: JSON.stringify({ request_token: requestToken }),
      }),
    onSettled: () => localStorage.removeItem(PENDING_ACCOUNT_KEY),
  });

  useEffect(() => {
    if (!accountId || !requestToken || loginStatus !== "success") return;
    completeMutation.mutate(accountId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, requestToken, loginStatus]);

  const connected = completeMutation.data?.connection_status === "connected";

  return (
    <div className="mx-auto max-w-md space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Zerodha Login</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 pt-4">
          {loginStatus !== "success" || !requestToken ? (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">Zerodha login did not complete</p>
              <p className="text-sm text-text-muted">No request token was returned. You can close this tab and try again.</p>
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
              <p className="text-sm font-medium text-text-primary">Zerodha Kite connected</p>
              <p className="text-sm text-text-muted">You can close this tab and return to Settings &gt; Brokers.</p>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-2 py-6 text-center">
              <XCircle className="h-6 w-6 text-negative" />
              <p className="text-sm font-medium text-text-primary">Connection failed</p>
              <p className="text-sm text-text-muted">Zerodha rejected the login. Close this tab and try again from Settings &gt; Brokers.</p>
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
