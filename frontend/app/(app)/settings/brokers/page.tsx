"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, LogIn } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
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
import type { BrokerAccountOut, HDFCLoginUrlOut, KiteLoginUrlOut } from "@/lib/types";

const KITE_PENDING_ACCOUNT_KEY = "tm_kite_pending_account_id";
const HDFC_PENDING_ACCOUNT_KEY = "tm_hdfc_pending_account_id";

// Brokers whose auth needs an interactive browser login after the initial
// api_key/api_secret connect step (registry.py's _INTERACTIVE_AUTH_BROKERS,
// mirrored here) -- Kotak Neo is NOT here: its TOTP+MPIN credentials
// authenticate in a single step, same as Delta.
const INTERACTIVE_AUTH_BROKERS = new Set(["zerodha_kite", "hdfc_securities"]);

const BROKER_LABELS: Record<string, string> = {
  zerodha_kite: "Zerodha",
  hdfc_securities: "HDFC Securities",
};

// Brokers that log in with a PIN and a TOTP secret -- no daily login --
// and the fields each one needs, with where to find them.
type CredentialField = { key: string; label: string; hint: string; secret?: boolean };
const PIN_TOTP_FIELDS: Record<string, CredentialField[]> = {
  angel_one: [
    { key: "api_key", label: "SmartAPI key", hint: "smartapi.angelone.in > My Apps" },
    { key: "client_code", label: "Client code", hint: "Your Angel One client ID" },
    { key: "pin", label: "Login PIN", hint: "The PIN you log in to Angel One with", secret: true },
    { key: "totp_secret", label: "TOTP secret", hint: "The key from TOTP setup, not a 6-digit code", secret: true },
  ],
  dhan: [
    { key: "client_id", label: "Client ID", hint: "Your Dhan client ID (Profile)" },
    { key: "pin", label: "Login PIN", hint: "The PIN you log in to Dhan with", secret: true },
    { key: "totp_secret", label: "TOTP secret", hint: "The key from TOTP setup, not a 6-digit code", secret: true },
  ],
};

function CredentialInputs({
  fields, values, onChange, required, keepHint,
}: {
  fields: CredentialField[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  required?: boolean;
  keepHint?: boolean;
}) {
  return (
    <>
      {fields.map((f) => (
        <div key={f.key} className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">{f.label}</label>
          <Input
            type={f.secret ? "password" : "text"}
            required={required}
            autoComplete="off"
            value={values[f.key] ?? ""}
            onChange={(e) => onChange({ ...values, [f.key]: e.target.value })}
            placeholder={keepHint ? "Leave blank to keep current" : f.hint}
          />
        </div>
      ))}
    </>
  );
}

function credentialsFrom(fields: CredentialField[], values: Record<string, string>): Record<string, string> {
  return Object.fromEntries(fields.map((f) => [f.key, (values[f.key] ?? "").trim()]));
}

function ConnectBrokerModal({ open, onClose, accounts }: { open: boolean; onClose: () => void; accounts: BrokerAccountOut[] }) {
  const { data: brokers } = useBrokers();
  const queryClient = useQueryClient();
  const [brokerCode, setBrokerCode] = useState("");
  const [label, setLabel] = useState("");
  const [environment, setEnvironment] = useState("paper");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [consumerKey, setConsumerKey] = useState("");
  const [mobileNumber, setMobileNumber] = useState("");
  const [ucc, setUcc] = useState("");
  const [totpSecret, setTotpSecret] = useState("");
  const [mpin, setMpin] = useState("");
  const [pinTotp, setPinTotp] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [confirmDuplicate, setConfirmDuplicate] = useState(false);

  const isKotakNeo = brokerCode === "kotak_neo";
  const pinTotpFields = PIN_TOTP_FIELDS[brokerCode];
  const existing = accounts.filter((a) => a.broker.code === brokerCode && a.environment === environment);
  // Kite's daily-expiry re-login only needs the SAME account's "Login with
  // Zerodha" button -- creating another "Connect Broker" row every day (the
  // exact bug this warning exists to stop) just piles up duplicates that
  // all use the identical, still-correct api_key/api_secret.
  const wouldDuplicate = brokerCode !== "" && existing.length > 0 && !confirmDuplicate;

  const connectMutation = useMutation({
    mutationFn: () =>
      apiFetch<BrokerAccountOut>("/api/v1/brokers/accounts", {
        method: "POST",
        body: JSON.stringify({
          broker_code: brokerCode,
          account_label: label,
          environment,
          credentials: isKotakNeo
            ? { consumer_key: consumerKey, mobile_number: mobileNumber, ucc, totp_secret: totpSecret, mpin }
            : pinTotpFields
              ? credentialsFrom(pinTotpFields, pinTotp)
              : { api_key: apiKey, api_secret: apiSecret },
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
          <Select required value={brokerCode} onChange={(e) => { setBrokerCode(e.target.value); setConfirmDuplicate(false); setPinTotp({}); }}>
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
          <Select value={environment} onChange={(e) => { setEnvironment(e.target.value); setConfirmDuplicate(false); }}>
            <option value="paper">Paper</option>
            <option value="live">Live</option>
          </Select>
        </div>

        {existing.length > 0 && (
          <div className="space-y-2 rounded-md border border-warning/30 bg-warning-soft px-3 py-2 text-sm text-warning">
            <p>
              You already have {existing.length === 1 ? "an account" : `${existing.length} accounts`} connected for this
              broker + environment ({existing.map((a) => a.account_label).join(", ")}). If this is a daily session
              re-login (Kite/HDFC sessions expire daily, same api_key/api_secret), close this and use{" "}
              <span className="font-medium">&quot;Login with...&quot;</span> on that existing row instead --
              creating another one here just adds a duplicate with identical credentials.
            </p>
            <label className="flex items-center gap-1.5 text-xs">
              <input type="checkbox" checked={confirmDuplicate} onChange={(e) => setConfirmDuplicate(e.target.checked)} />
              This is genuinely a separate account (e.g. a different Zerodha client ID) -- connect anyway
            </label>
          </div>
        )}

        {isKotakNeo ? (
          <>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Consumer key</label>
              <Input value={consumerKey} onChange={(e) => setConsumerKey(e.target.value)} placeholder="From Kotak Neo app/web: Invest > Trade API" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Mobile number</label>
              <Input value={mobileNumber} onChange={(e) => setMobileNumber(e.target.value)} placeholder="Registered mobile, with country code" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">UCC</label>
              <Input value={ucc} onChange={(e) => setUcc(e.target.value.toUpperCase())} placeholder="Unique Client Code (Profile section)" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">TOTP secret</label>
              <Input type="password" value={totpSecret} onChange={(e) => setTotpSecret(e.target.value)} placeholder="From TOTP registration on Kotak Neo's site" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">MPIN</label>
              <Input type="password" value={mpin} onChange={(e) => setMpin(e.target.value)} />
            </div>
          </>
        ) : pinTotpFields ? (
          <CredentialInputs fields={pinTotpFields} values={pinTotp} onChange={setPinTotp} required />
        ) : (
          <>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">API key</label>
              <Input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="App api_key" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">API secret</label>
              <Input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} />
            </div>
          </>
        )}

        <p className="text-xs text-text-muted">
          All brokers use real adapters -- real API credentials are required. Delta Exchange, Kotak Neo, Angel One
          and Dhan authenticate immediately. Angel One and Dhan are connected for login and funds only for now --
          orders can&apos;t be placed through them yet. Zerodha Kite and HDFC Securities need one more step after this: an interactive
          browser login (neither supports key/secret-only auth) -- you&apos;ll get a &quot;Login with...&quot; button
          for the account once it&apos;s created. Kotak Neo needs TOTP registration completed on their own site first
          (one-time, scan a QR code into an authenticator app) -- the TOTP secret above is that same registration
          secret, not a live 6-digit code.
        </p>

        {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={connectMutation.isPending || wouldDuplicate}>
            {connectMutation.isPending ? "Connecting..." : "Connect"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function InteractiveLoginButton({ accountId, brokerCode }: { accountId: string; brokerCode: string }) {
  const isKite = brokerCode === "zerodha_kite";
  const pendingKey = isKite ? KITE_PENDING_ACCOUNT_KEY : HDFC_PENDING_ACCOUNT_KEY;
  const loginUrlPath = isKite ? "kite/login-url" : "hdfc/login-url";
  const label = `Login with ${BROKER_LABELS[brokerCode] ?? brokerCode}`;

  const loginMutation = useMutation({
    mutationFn: () => apiFetch<KiteLoginUrlOut | HDFCLoginUrlOut>(`/api/v1/brokers/accounts/${accountId}/${loginUrlPath}`),
    onSuccess: (data) => {
      localStorage.setItem(pendingKey, accountId);
      window.open(data.login_url, "_blank", "noopener,noreferrer");
    },
  });

  return (
    <Button variant="secondary" size="sm" onClick={() => loginMutation.mutate()} disabled={loginMutation.isPending}>
      <LogIn className="h-3.5 w-3.5" /> {loginMutation.isPending ? "Opening..." : label}
    </Button>
  );
}

function EditBrokerAccountModal({ account, onClose }: { account: BrokerAccountOut | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [label, setLabel] = useState(account?.account_label ?? "");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [consumerKey, setConsumerKey] = useState("");
  const [mobileNumber, setMobileNumber] = useState("");
  const [ucc, setUcc] = useState("");
  const [totpSecret, setTotpSecret] = useState("");
  const [mpin, setMpin] = useState("");
  const [pinTotp, setPinTotp] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const isKotakNeo = account?.broker.code === "kotak_neo";
  const pinTotpFields = account ? PIN_TOTP_FIELDS[account.broker.code] : undefined;
  const pinTotpTouched = Object.values(pinTotp).some((v) => v.trim());

  // Re-seed the label whenever a different row is opened for editing --
  // the modal instance is shared across rows, only mounted while one is open.
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  if (account && account.id !== openedFor) {
    setOpenedFor(account.id);
    setLabel(account.account_label);
    setApiKey("");
    setApiSecret("");
    setConsumerKey("");
    setMobileNumber("");
    setUcc("");
    setTotpSecret("");
    setMpin("");
    setPinTotp({});
    setError(null);
  }

  const kotakFieldsTouched = consumerKey || mobileNumber || ucc || totpSecret || mpin;

  const updateMutation = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { account_label: label };
      // All fields for a broker required together -- a partial credential
      // update would silently corrupt the stored set (e.g. new key with
      // the old secret).
      if (isKotakNeo) {
        if (kotakFieldsTouched) body.credentials = { consumer_key: consumerKey, mobile_number: mobileNumber, ucc, totp_secret: totpSecret, mpin };
      } else if (pinTotpFields) {
        if (pinTotpTouched) body.credentials = credentialsFrom(pinTotpFields, pinTotp);
      } else if (apiKey || apiSecret) {
        body.credentials = { api_key: apiKey, api_secret: apiSecret };
      }
      return apiFetch(`/api/v1/brokers/accounts/${account!.id}`, { method: "PATCH", body: JSON.stringify(body) });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["broker-accounts"] });
      onClose();
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "Failed to update account"),
  });

  if (!account) return null;

  return (
    <Modal open={!!account} onClose={onClose} title={`Edit ${account.account_label}`}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          if (isKotakNeo) {
            if (kotakFieldsTouched && !(consumerKey && mobileNumber && ucc && totpSecret && mpin)) {
              setError("Fill in all five fields together, or leave all blank to keep the current ones.");
              return;
            }
          } else if (pinTotpFields) {
            if (pinTotpTouched && pinTotpFields.some((f) => !(pinTotp[f.key] ?? "").trim())) {
              setError(`Fill in all ${pinTotpFields.length} fields together, or leave all blank to keep the current ones.`);
              return;
            }
          } else {
            if (apiKey.trim() !== apiKey || apiSecret.trim() !== apiSecret) {
              setError("API key/secret has leading or trailing whitespace -- remove it before saving.");
              return;
            }
            if ((apiKey && !apiSecret) || (!apiKey && apiSecret)) {
              setError("Enter both API key and API secret together, or leave both blank to keep the current ones.");
              return;
            }
          }
          updateMutation.mutate();
        }}
        className="space-y-4"
      >
        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Broker</label>
          <p className="text-sm text-text-primary">{account.broker.name} ({account.environment})</p>
        </div>

        <div className="space-y-1.5">
          <label className="text-sm font-medium text-text-secondary">Account label</label>
          <Input required value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>

        {isKotakNeo ? (
          <>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Consumer key</label>
              <Input value={consumerKey} onChange={(e) => setConsumerKey(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Mobile number</label>
              <Input value={mobileNumber} onChange={(e) => setMobileNumber(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">UCC</label>
              <Input value={ucc} onChange={(e) => setUcc(e.target.value.toUpperCase())} placeholder="Leave blank to keep current" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">TOTP secret</label>
              <Input type="password" value={totpSecret} onChange={(e) => setTotpSecret(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">MPIN</label>
              <Input type="password" value={mpin} onChange={(e) => setMpin(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <p className="text-xs text-text-muted">
              Only fill these in if you actually need to correct them -- all five must be provided together, or leave
              all blank to keep the current ones. Kotak Neo has no separate daily re-login step.
            </p>
          </>
        ) : pinTotpFields ? (
          <>
            <CredentialInputs fields={pinTotpFields} values={pinTotp} onChange={setPinTotp} keepHint />
            <p className="text-xs text-text-muted">
              Only fill these in to correct them -- all {pinTotpFields.length} together, or leave all blank to keep the
              current ones. {account.broker.name} logs in by itself with the TOTP secret; there is no daily re-login.
            </p>
          </>
        ) : (
          <>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">API key</label>
              <Input value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">API secret</label>
              <Input type="password" value={apiSecret} onChange={(e) => setApiSecret(e.target.value)} placeholder="Leave blank to keep current" />
            </div>
            <p className="text-xs text-text-muted">
              Only fill in API key/secret if you actually need to correct them -- for the ordinary daily re-login
              (session expiry, credentials unchanged), close this and use &quot;Login with...&quot; on the row instead.
            </p>
          </>
        )}

        {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{error}</div>}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" disabled={updateMutation.isPending}>
            {updateMutation.isPending ? "Saving..." : "Save"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function InteractiveSessionExpiredBanner({ accounts }: { accounts: BrokerAccountOut[] }) {
  const expired = accounts.filter((a) => INTERACTIVE_AUTH_BROKERS.has(a.broker.code) && a.connection_status === "error");
  if (!expired.length) return null;
  return (
    <div className="flex items-start gap-3 rounded-md border border-negative/30 bg-negative-soft px-4 py-3 text-sm text-negative">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="space-y-1">
        {expired.map((a) => (
          <p key={a.id}>
            <span className="font-medium">{a.account_label}</span>: {a.connection_last_error || "Broker session lost."} These
            brokers&apos; sessions expire daily (no refresh token) -- use &quot;Login with {BROKER_LABELS[a.broker.code] ?? a.broker.name}&quot; below to reconnect.
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
  const [editingAccount, setEditingAccount] = useState<BrokerAccountOut | null>(null);
  const canManage = hasRole("administrator", "trader");

  const disconnectMutation = useMutation({
    mutationFn: (accountId: string) =>
      apiFetch(`/api/v1/brokers/accounts/${accountId}/disconnect`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["broker-accounts"] }),
  });

  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteMutation = useMutation({
    mutationFn: (accountId: string) => apiFetch(`/api/v1/brokers/accounts/${accountId}`, { method: "DELETE" }),
    onSuccess: () => {
      setDeleteError(null);
      queryClient.invalidateQueries({ queryKey: ["broker-accounts"] });
    },
    onError: (err) => setDeleteError(err instanceof ApiError ? err.message : "Failed to delete account"),
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Broker Connections</h1>
          <p className="text-sm text-text-muted">
            Zerodha Kite, Delta Exchange, HDFC Securities, Kotak Neo, Angel One and Dhan all use real adapters --
            HMAC-signed for Delta, session-token auth via interactive login for Kite and HDFC, TOTP for Kotak Neo,
            Angel One and Dhan. Angel One and Dhan are connected for login and funds only for now.
          </p>
        </div>
        {canManage && <Button onClick={() => setModalOpen(true)}>Connect Broker</Button>}
      </div>

      {accounts && <InteractiveSessionExpiredBanner accounts={accounts} />}
      {deleteError && <div className="rounded-md bg-negative-soft px-3 py-2 text-sm text-negative">{deleteError}</div>}

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
                    <Td>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {account.broker.name}
                        {account.broker.supports_trading === false && (
                          <Badge tone="neutral" title="Connected for login and funds only -- trading through it isn't enabled yet">
                            Login &amp; funds only
                          </Badge>
                        )}
                      </div>
                    </Td>
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
                          {INTERACTIVE_AUTH_BROKERS.has(account.broker.code) && account.connection_status !== "connected" && (
                            <InteractiveLoginButton accountId={account.id} brokerCode={account.broker.code} />
                          )}
                          <Button variant="ghost" size="sm" onClick={() => setEditingAccount(account)}>
                            Edit
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={account.connection_status === "disconnected" || disconnectMutation.isPending}
                            onClick={() => disconnectMutation.mutate(account.id)}
                          >
                            Disconnect
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={deleteMutation.isPending}
                            onClick={() => {
                              if (window.confirm(`Delete "${account.account_label}" (${account.broker.name}, ${account.environment})? This cannot be undone.`)) {
                                deleteMutation.mutate(account.id);
                              }
                            }}
                          >
                            Delete
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

      <ConnectBrokerModal open={modalOpen} onClose={() => setModalOpen(false)} accounts={accounts ?? []} />
      <EditBrokerAccountModal account={editingAccount} onClose={() => setEditingAccount(null)} />
    </div>
  );
}
