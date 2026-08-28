"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Modal } from "@/components/ui/modal";
import { Select } from "@/components/ui/select";
import { ConnectionStatusBadge } from "@/components/ui/status-badge";
import { Table, Tbody, Td, Th, Thead } from "@/components/ui/table";
import { apiFetch, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { useBrokerAccounts, useBrokers } from "@/lib/hooks";
import type { BrokerAccountOut } from "@/lib/types";

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
          <Input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Mock broker: any value works" />
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">API secret</label>
          <Input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
        </div>

        <p className="text-xs text-text-muted">
          Phase 1 uses a simulated broker adapter — no real orders are placed and no live credentials are required.
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

export default function BrokersSettingsPage() {
  const { hasRole } = useAuth();
  const { data: accounts, isLoading } = useBrokerAccounts();
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
          <p className="text-sm text-text-muted">Zerodha Kite and Delta Exchange, backed by a simulated adapter in Phase 1.</p>
        </div>
        {canManage && <Button onClick={() => setModalOpen(true)}>Connect Broker</Button>}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Connected Accounts</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <p className="p-5 text-sm text-text-muted">Loading...</p>
          ) : !accounts?.length ? (
            <p className="p-5 text-sm text-text-muted">No broker accounts connected yet.</p>
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
                    </Td>
                    {canManage && (
                      <Td className="text-right">
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={account.connection_status === "disconnected" || disconnectMutation.isPending}
                          onClick={() => disconnectMutation.mutate(account.id)}
                        >
                          Disconnect
                        </Button>
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
