"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/data-state";
import { useAllLiveTrades, useBrokerAccounts, useBrokerBalance, useLiveDeployments, useQuotes } from "@/lib/hooks";
import type { BrokerAccountOut } from "@/lib/types";

function SummaryCard({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return (
    <Card>
      <CardContent className="space-y-1 p-4">
        <p className="text-xs text-text-muted">{label}</p>
        <p className={`font-financial text-2xl font-semibold ${tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "text-text-primary"}`}>
          {value}
        </p>
      </CardContent>
    </Card>
  );
}

function BrokerBalanceCard({ account }: { account: BrokerAccountOut }) {
  const { data: balance, isLoading, isError, error } = useBrokerBalance(account.id);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">
          {account.broker.name} -- {account.account_label}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <p className="text-xs text-negative">
            {error instanceof Error ? error.message : "Could not fetch real balance from the broker."}
          </p>
        ) : balance ? (
          <>
            <p className="font-financial text-xl font-semibold text-text-primary">
              {balance.available_margin.toLocaleString(undefined, { maximumFractionDigits: 2 })} {balance.currency}
            </p>
            <p className="text-xs text-text-muted">
              {balance.used_margin.toLocaleString(undefined, { maximumFractionDigits: 2 })} {balance.currency} used
            </p>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function PortfolioPage() {
  const { data: deployments } = useLiveDeployments();
  const { data: trades } = useAllLiveTrades();
  const { data: brokerAccounts } = useBrokerAccounts();

  const open = (deployments ?? []).filter((d) => d.open_position);
  const { data: quotes } = useQuotes(open.map((d) => d.instrument_id));
  const lastCloseByInstrument = new Map((quotes ?? []).map((q) => [q.instrument_id, q.prev_close]));

  const unrealizedTotal = open.reduce((sum, d) => {
    const lastClose = lastCloseByInstrument.get(d.instrument_id);
    if (lastClose == null || !d.open_position) return sum;
    return sum + (lastClose - d.open_position.avg_entry_price) * d.open_position.quantity;
  }, 0);

  const today = new Date().toDateString();
  const realizedToday = (trades ?? []).filter((t) => new Date(t.exit_ts).toDateString() === today).reduce((sum, t) => sum + t.pnl, 0);
  const realizedAllTime = (trades ?? []).reduce((sum, t) => sum + t.pnl, 0);

  const connectedLiveAccounts = (brokerAccounts ?? []).filter((a) => a.environment === "live" && a.connection_status === "connected");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-text-primary">Portfolio</h1>
        <p className="text-sm text-text-muted">Real capital and P&amp;L across every connected live broker account.</p>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryCard label="Realized P&L (today)" value={`${realizedToday >= 0 ? "+" : ""}${realizedToday.toFixed(2)}`} tone={realizedToday >= 0 ? "positive" : "negative"} />
        <SummaryCard label="Realized P&L (all-time)" value={`${realizedAllTime >= 0 ? "+" : ""}${realizedAllTime.toFixed(2)}`} tone={realizedAllTime >= 0 ? "positive" : "negative"} />
        <SummaryCard label="Unrealized P&L (open positions)" value={`${unrealizedTotal >= 0 ? "+" : ""}${unrealizedTotal.toFixed(2)}`} tone={unrealizedTotal >= 0 ? "positive" : "negative"} />
        <SummaryCard label="Open Positions" value={String(open.length)} />
      </div>

      <div className="space-y-3">
        <h2 className="text-sm font-semibold text-text-primary">Connected Broker Accounts</h2>
        {!connectedLiveAccounts.length ? (
          <p className="text-sm text-text-muted">No connected live broker accounts yet -- connect one in Settings &gt; Brokers.</p>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {connectedLiveAccounts.map((account) => (
              <BrokerBalanceCard key={account.id} account={account} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
