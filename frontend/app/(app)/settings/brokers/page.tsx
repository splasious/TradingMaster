"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, LogIn } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/data-state";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { ConnectionStatusBadge } from "@/components/ui/status-badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBrokerAccounts, useBrokers } from "@/lib/hooks";
import type { BrokerAccountOut, KiteLoginUrlOut } from "@/lib/types";

const PENDING_ACCOUNT_KEY = "tm_kite_pending_account_id";

function ConnectBrokerModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { data: brokers } = useBrokers();
  const queryClient = useQueryClient();
  const [brokerCode, setBrokerCode] = useState("");
  const [label, setLabel] = useState("");
  const [environment, setEnvironment] = useState("paper");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [error, setError] = useState<string | null>(null);

  const connectMutation = useMutation({
    mutationFn: () =>
      apiFetch<BrokerAccountOut>("/api/v1/brokers/accounts", {
        method: "POST",
        body: JSON.stringify({
          broker_code: brokerCode,
          account_label: label,
          environment,
          credentials: { api_key: apiKey, api_secret: apiSecret },
        }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["broker-accounts"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to connect broker"),
  });

  return (
    <Modal open={open} onClose={onClose} title="Connect Broker">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          connectMutation.mutate();
        }}
        className="space-y-4"
      >
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Broker</label>
          <Select required value={brokerCode} onChange={(e) => setBrokerCode(e.target.value)}>
            <option value="" disabled>
              Select a broker
            </option>
            {brokers?.map((b) => (
              <option key={b.code} value={b.code}>
                {b.name}
              </option>
            ))}
          </Select>
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Account label</label>
          <Input required value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. My Zerodha" />
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Environment</label>
          <Select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="paper">Paper</option>
            <option value="live">Live</option>
          </Select>
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">API key</label>
          <Input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Kite Connect app api_key" />
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">API secret</label>
          <Input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
        </div>

        <p className="text-xs text-text-muted">
          Both brokers use real adapters -- real API credentials are required. Delta Exchange authenticates immediately.
          Zerodha Kite needs one more step after this: an interactive browser login (Kite Connect doesn&apos;t support
          key/secret-only auth) -- you&apos;ll get a &quot;Login with Zerodha&quot; button for the account once it&apos;s created.
        </p>

        {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={connectMutation.isPending}>
            {connectMutation.isPending ? "Connecting..." : "Connect"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function LoginWithZerodhaButton({ accountId }: { accountId: string }) {
  const loginMutation = useMutation({
    mutationFn: () => apiFetch<KiteLoginUrlOut>(`/api/v1/brokers/accounts/${accountId}/kite/login-url`),
    onSuccess: (data) => {
      localStorage.setItem(PENDING_ACCOUNT_KEY, accountId);
      window.open(data.login_url, "_blank", "noopener,noreferrer");
    },
  });

  return (
    <Button variant="secondary" size="sm" onClick={() => loginMutation.mutate()} disabled={loginMutation.isPending}>
      <LogIn className="h-3.5 w-3.5" /> {loginMutation.isPending ? "Opening..." : "Login with Zerodha"}
    </Button>
  );
}

function KiteSessionExpiredBanner({ accounts }: { accounts: BrokerAccountOut[] }) {
  const expired = accounts.filter((a) => a.broker.code === "zerodha_kite" && a.connection_status === "error");
  if (!expired.length) return null;
  return (
    <div className="flex items-start gap-3 rounded-md border border-negative/30 bg-negative-soft px-4 py-3 text-sm text-negative">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="space-y-1">
        {expired.map((a) => (
          <p key={a.id}>
            <span className="font-medium">{a.account_label}</span>: {a.connection_last_error || "Zerodha Kite session lost."} Kite&apos;s
            daily session expires every day (~6am IST, no refresh token) -- use &quot;Login with Zerodha&quot; below to reconnect.
          </p>
        ))}
      </div>
    </div>
  );
}

export default function BrokersSettingsPage() {
  const { hasRole } = useAuth();
  const { data: accounts, isLoading, isError } = useBrokerAccounts();
  const queryClient = useQueryClient();
  const [modalOpen, setModalOpen] = useState(false);
  const canManage = hasRole("administrator", "trader");

  const disconnectMutation = useMutation({
    mutationFn: (accountId: string) =>
      apiFetch(`/api/v1/brokers/accounts/${accountId}/disconnect`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["broker-accounts"] }),
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Broker Connections</h1>
          <p className="text-sm text-text-muted">
            Zerodha Kite and Delta Exchange both use real adapters -- HMAC-signed for Delta, session-token auth via
            interactive login for Kite.
          </p>
        </div>
        {canManage && <Button onClick={() => setModalOpen(true)}>Connect Broker</Button>}
      </div>

      {accounts && <KiteSessionExpiredBanner accounts={accounts} />}

      <Card>
        <CardHeader>
          <CardTitle>Connected Accounts</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <LoadingState />
          ) : isError ? (
            <ErrorState description="Could not load broker accounts." />
          ) : !accounts?.length ? (
            <EmptyState title="No broker accounts connected yet" />
          ) : (
            <Table>
              <Thead>
                <tr>
                  <Th>Broker</Th>
                  <Th>Label</Th>
                  <Th>Environment</Th>
                  <Th>Status</Th>
                  {canManage && <Th />}
                </tr>
              </Thead>
              <Tbody>
                {accounts.map((account) => (
                  <tr key={account.id}>
                    <Td>{account.broker.name}</Td>
                    <Td>{account.account_label}</Td>
                    <Td className="capitalize">{account.environment}</Td>
                    <Td>
                      <ConnectionStatusBadge status={account.connection_status} />
                      {account.connection_status === "error" && account.connection_last_error && (
                        <p className="mt-1 max-w-xs text-xs text-text-muted">{account.connection_last_error}</p>
                      )}
                    </Td>
                    {canManage && (
                      <Td className="text-right">
                        <div className="flex justify-end gap-1">
                          {account.broker.code === "zerodha_kite" && account.connection_status !== "connected" && (
                            <LoginWithZerodhaButton accountId={account.id} />
                          )}
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={account.connection_status === "disconnected" || disconnectMutation.isPending}
                            onClick={() => disconnectMutation.mutate(account.id)}
                          >
                            Disconnect
                          </Button>
                        </div>
                      </Td>
                    )}
                  </tr>
                ))}
              </Tbody>
            </Table>
          )}
        </CardContent>
      </Card>

      <ConnectBrokerModal open={modalOpen} onClose={() => setModalOpen(false)} />
    </div>
  );
}
