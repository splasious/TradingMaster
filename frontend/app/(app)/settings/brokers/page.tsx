"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, LogIn, XCircle } from "lucide-react";
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
import { istDate } from "@/lib/time";
import type { BrokerAccountOut, BrokerTestOut, HDFCLoginUrlOut, KiteLoginUrlOut, ServerIpOut } from "@/lib/types";

const KITE_PENDING_ACCOUNT_KEY = "tm_kite_pending_account_id";
const HDFC_PENDING_ACCOUNT_KEY = "tm_hdfc_pending_account_id";

// Brokers whose auth needs an interactive browser login after the initial
// api_key/api_secret connect step (registry.py's _INTERACTIVE_AUTH_BROKERS,
// mirrored here) -- Kotak Neo is NOT here: its TOTP+MPIN credentials
// authenticate in a single step.
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
          All brokers use real adapters -- real API credentials are required. Kotak Neo, Angel One and Dhan
          authenticate immediately. Angel One and Dhan place orders for live strategies only (after the broker
          test) -- manual orders can&apos;t go through them yet. Zerodha Kite and HDFC Securities need one more step after this: an interactive
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

function ServerIpCard() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["live-native-server-ip"],
    queryFn: () => apiFetch<ServerIpOut>("/api/v1/live-native/server-ip"),
    staleTime: 10 * 60 * 1000,
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Static IP for live orders</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <p className="text-text-muted">
          Brokers accept API orders only from a static IP you have registered with them (SEBI&apos;s rules, since 1 April
          2026). Register this server&apos;s IP with each broker you&apos;ll trade live through: Zerodha (Kite Connect
          developer console), Dhan (DhanHQ API page), Angel One (SmartAPI app), Kotak Neo (Trade API settings).
        </p>
        {isLoading ? (
          <p className="text-text-muted">Checking...</p>
        ) : isError || (!data?.ipv4 && !data?.ipv6) ? (
          <p className="text-warning">Couldn&apos;t find this server&apos;s public IP right now -- try again in a minute.</p>
        ) : (
          <div className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-text-primary">
            {data?.ipv4 && <span>IPv4 {data.ipv4}</span>}
            {data?.ipv6 && <span className="break-all">IPv6 {data.ipv6}</span>}
          </div>
        )}
        {data?.ipv6 && (
          <p className="text-xs text-text-muted">
            This server also has IPv6. A broker reached over IPv6 sees that address, not the IPv4 one -- register both where
            the broker allows two.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function BrokerTestModal({ account, onClose }: { account: BrokerAccountOut | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [symbol, setSymbol] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [report, setReport] = useState<BrokerTestOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  if (account && account.id !== openedFor) {
    setOpenedFor(account.id);
    setSymbol("");
    setUnderstood(false);
    setReport(null);
    setError(null);
  }

  const testMutation = useMutation({
    mutationFn: () =>
      apiFetch<BrokerTestOut>("/api/v1/live-native/broker-test", {
        method: "POST",
        body: JSON.stringify({ broker_account_id: account!.id, symbol: symbol.trim() }),
      }),
    onSuccess: (data) => {
      setReport(data);
      queryClient.invalidateQueries({ queryKey: ["broker-accounts"] });
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : "The test couldn't run"),
  });

  if (!account) return null;

  return (
    <Modal open={!!account} onClose={onClose} title={`Test ${account.account_label} for live strategies`}>
      <div className="space-y-4 text-sm">
        <p className="text-text-secondary">
          This places two real orders in this account: it buys 1 share of the stock you choose, intraday, and sells it
          again -- checking the contract names, the fill, the price and the position at every step. It costs the
          share&apos;s spread and two orders&apos; brokerage. Run it while the market is open, before 15:00. Live strategies
          only run on an account that passed.
        </p>

        {!report && (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              setError(null);
              testMutation.mutate();
            }}
          >
            <div className="space-y-1.5">
              <label className="text-sm font-medium text-text-secondary">Stock (NSE, under ₹1,000)</label>
              <Input required value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} placeholder="e.g. IDEA" />
            </div>
            <label className="flex items-start gap-2 text-xs text-text-secondary">
              <input type="checkbox" className="mt-0.5" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
              I understand this buys and sells 1 share with real money in {account.broker.name}.
            </label>
            {error && <div className="rounded-md bg-negative-soft px-3 py-2 text-negative">{error}</div>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="secondary" onClick={onClose}>
                Cancel
              </Button>
              <Button type="submit" disabled={!understood || !symbol.trim() || testMutation.isPending}>
                {testMutation.isPending ? "Testing... (up to a minute)" : "Buy and sell 1 share"}
              </Button>
            </div>
          </form>
        )}

        {report && (
          <div className="space-y-3">
            {report.still_held && (
              <div className="flex items-start gap-2 rounded-md border border-negative/30 bg-negative-soft px-3 py-2 text-negative">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <p>{report.still_held}</p>
              </div>
            )}
            <p className={report.passed ? "font-medium text-positive" : "font-medium text-negative"}>
              {report.passed ? "Passed -- live strategies can use this account." : "Not passed -- live strategies can't use this account yet."}
            </p>
            <ul className="space-y-1.5">
              {report.steps.map((s, i) => (
                <li key={i} className="flex items-start gap-2">
                  {s.ok ? (
                    <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-positive" aria-label="passed" />
                  ) : (
                    <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-negative" aria-label="failed" />
                  )}
                  <span>
                    <span className="font-medium text-text-primary">{s.name}</span>{" "}
                    <span className="text-text-secondary">{s.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
            {report.contracts.length > 0 && (
              <div className="overflow-x-auto">
                <Table>
                  <Thead>
                    <tr>
                      <Th>Here</Th>
                      <Th>At the broker</Th>
                    </tr>
                  </Thead>
                  <Tbody>
                    {report.contracts.map((c) => (
                      <tr key={c.ours}>
                        <Td className="font-mono text-xs">{c.ours}</Td>
                        <Td className="text-xs">
                          {c.ok ? (
                            <span className="font-mono">
                              {c.broker_symbol}
                              {c.broker_id ? ` (${c.broker_id})` : ""}
                              {c.lot_size ? `, lot ${c.lot_size}` : ""}
                            </span>
                          ) : (
                            <span className="text-negative">{c.error}</span>
                          )}
                        </Td>
                      </tr>
                    ))}
                  </Tbody>
                </Table>
              </div>
            )}
            <div className="flex justify-end">
              <Button type="button" variant="secondary" onClick={onClose}>
                Close
              </Button>
            </div>
          </div>
        )}
      </div>
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
  const [testingAccount, setTestingAccount] = useState<BrokerAccountOut | null>(null);
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
            Zerodha Kite, HDFC Securities, Kotak Neo, Angel One and Dhan all use real adapters -- session-token
            auth via interactive login for Kite and HDFC, TOTP for Kotak Neo, Angel One and Dhan. Live strategies trade through
            Zerodha, Dhan, Angel One and Kotak Neo, on an account that passed the broker test.
          </p>
        </div>
        {canManage && <Button onClick={() => setModalOpen(true)}>Connect Broker</Button>}
      </div>

      {accounts && <InteractiveSessionExpiredBanner accounts={accounts} />}
      <ServerIpCard />
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
                        {account.broker.supports_trading === false && !account.broker.supports_live_strategies && (
                          <Badge tone="neutral" title="Connected for login and funds only -- trading through it isn't enabled yet">
                            Login &amp; funds only
                          </Badge>
                        )}
                        {account.broker.supports_trading === false && account.broker.supports_live_strategies && (
                          <Badge tone="neutral" title="Orders go through it for live strategies only -- not manual orders yet">
                            Live strategies only
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
                      {account.environment === "live" && account.broker.supports_live_strategies && (
                        <div className="mt-1">
                          {account.live_verified_at ? (
                            <Badge tone="positive" title="Passed the broker test -- live strategies can use it">
                              Tested {istDate(account.live_verified_at)}
                            </Badge>
                          ) : (
                            <Badge tone="neutral" title="Live strategies run only on an account that passed the broker test">
                              Not tested for live
                            </Badge>
                          )}
                        </div>
                      )}
                    </Td>
                    {canManage && (
                      <Td className="text-right">
                        <div className="flex justify-end gap-1">
                          {INTERACTIVE_AUTH_BROKERS.has(account.broker.code) && account.connection_status !== "connected" && (
                            <InteractiveLoginButton accountId={account.id} brokerCode={account.broker.code} />
                          )}
                          {account.environment === "live" && account.broker.supports_live_strategies && (
                            <Button variant="ghost" size="sm" onClick={() => setTestingAccount(account)}>
                              Test
                            </Button>
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
      <BrokerTestModal account={testingAccount} onClose={() => setTestingAccount(null)} />
    </div>
  );
}
