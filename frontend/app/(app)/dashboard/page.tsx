"use client";

import { useMemo, useState } from "react";

import { PriceChart } from "@/components/charts/price-chart";
import { ZerodhaDataCard } from "@/components/dashboard/zerodha-data-card";
import { OrderTicket } from "@/components/trading/order-ticket";
import { PaperPositionsTable } from "@/components/trading/paper-positions-table";
import { WatchlistPanel } from "@/components/trading/watchlist-panel";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LoadingState } from "@/components/ui/data-state";
import { useAuth } from "@/lib/auth-context";
import {
  useAllNativeTrades,
  useAllPaperTrades,
  useBrokerAccounts,
  useBrokerBalance,
  useChartCandles,
  useInstruments,
  useNativeDeployments,
  usePaperDeployments,
  usePaperPortfolios,
  useSystemHealth,
} from "@/lib/hooks";
import type { InstrumentOut } from "@/lib/types";
import { useMarketDataSocket } from "@/lib/ws";

const CHART_TIMEFRAMES = ["5m", "15m", "60m", "1d"] as const;

/** NIFTY 50 / NIFTY BANK live tick + %change -- both are real index
 * instruments in the catalog (confirmed against the live DB; NSE has no
 * "INDIA VIX" instrument on file here, so it's left out entirely rather
 * than shown as a fabricated static number). */
function IndexTicker({ symbol }: { symbol: string }) {
  const { data: matches } = useInstruments(symbol, "NSE", 5);
  const instrument = matches?.find((i) => i.symbol === symbol) ?? null;
  const { prices } = useMarketDataSocket(instrument ? [instrument.id] : []);
  const tick = instrument ? prices[instrument.id] : undefined;

  return (
    <Card>
      <CardContent className="flex items-center justify-between py-3">
        <div>
          <p className="text-xs font-medium text-text-muted">{symbol}</p>
          <p className="font-financial text-lg font-semibold text-text-primary">
            {tick ? tick.price.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--"}
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

function StatCard({ label, value, tone }: { label: string; value: string; tone?: "positive" | "negative" }) {
  return (
    <Card>
      <CardContent className="py-3">
        <p className="text-xs font-medium text-text-muted">{label}</p>
        <p
          className={`font-financial text-lg font-semibold ${
            tone === "positive" ? "text-positive" : tone === "negative" ? "text-negative" : "text-text-primary"
          }`}
        >
          {value}
        </p>
      </CardContent>
    </Card>
  );
}

/** Aggregates Total Equity / Day P&L / Unrealized P&L the same way
 * app/(app)/paper-trading/page.tsx's PortfolioCard does (realized P&L
 * today from trade history + every open position's unrealized P&L, both
 * regular and Advanced Python/native) -- deliberately not the portfolio
 * API's own equity/unrealized_pnl fields, per that component's own
 * reasoning: summing the same live-streaming rows already rendered
 * elsewhere avoids two independent snapshots disagreeing by a few
 * seconds. Combined across every portfolio here, rather than
 * PortfolioCard's single-portfolio scope. */
function usePortfolioSummary() {
  const { data: portfolios } = usePaperPortfolios();
  const { data: deployments } = usePaperDeployments();
  const { data: nativeDeployments } = useNativeDeployments();
  const { data: allTrades } = useAllPaperTrades();
  const { data: allNativeTrades } = useAllNativeTrades();

  return useMemo(() => {
    const cash = (portfolios ?? []).reduce((sum, p) => sum + p.cash, 0);

    const openRegular = (deployments ?? []).filter((d) => d.open_position).map((d) => d.open_position!);
    const regularUnrealized = openRegular.reduce((sum, p) => sum + (p.unrealized_pnl ?? 0), 0);
    const regularValue = openRegular.reduce((sum, p) => sum + p.quantity * (p.current_price ?? p.avg_entry_price), 0);

    const nativeUnrealized = (nativeDeployments ?? [])
      .filter((d) => d.position)
      .reduce((sum, d) => sum + (d.position!.unrealized_pnl ?? 0), 0);
    const holdingsUnrealized = (nativeDeployments ?? [])
      .flatMap((d) => d.holdings ?? [])
      .reduce((sum, l) => sum + (l.current_price != null ? (l.current_price - l.entry_price) * l.quantity : 0), 0);

    const today = new Date().toDateString();
    const realizedToday =
      (allTrades ?? []).filter((t) => new Date(t.exit_ts).toDateString() === today).reduce((sum, t) => sum + t.pnl, 0) +
      (allNativeTrades ?? []).filter((t) => new Date(t.closed_at).toDateString() === today).reduce((sum, t) => sum + t.pnl, 0);

    const unrealizedTotal = regularUnrealized + nativeUnrealized + holdingsUnrealized;
    const equity = cash + regularValue;
    const dayPnl = realizedToday + unrealizedTotal;

    return { cash, equity, unrealizedTotal, dayPnl, loaded: portfolios !== undefined };
  }, [portfolios, deployments, nativeDeployments, allTrades, allNativeTrades]);
}

export default function DashboardPage() {
  const { user } = useAuth();
  const summary = usePortfolioSummary();
  const { data: health } = useSystemHealth();
  const { data: brokerAccounts } = useBrokerAccounts();
  const liveAccount = brokerAccounts?.find((a) => a.environment === "live" && a.connection_status === "connected");
  const { data: liveBalance } = useBrokerBalance(liveAccount?.id ?? null);

  const [selected, setSelected] = useState<InstrumentOut | null>(null);
  const [timeframe, setTimeframe] = useState<(typeof CHART_TIMEFRAMES)[number]>("15m");
  const { data: niftyMatches } = useInstruments("NIFTY 50", "NSE", 5);
  const defaultInstrument = niftyMatches?.find((i) => i.symbol === "NIFTY 50") ?? null;
  const chartInstrument = selected ?? defaultInstrument;
  const { data: candles, isLoading: candlesLoading } = useChartCandles(chartInstrument?.id ?? null, timeframe);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-text-primary">Welcome, {user?.full_name}</h1>
          <p className="text-sm text-text-muted">Paper trading overview -- live prices, real positions, real orders.</p>
        </div>
        {health && (
          <span className={`text-xs font-medium ${health.status === "healthy" ? "text-positive" : "text-critical"}`}>
            System {health.status}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-6">
        <IndexTicker symbol="NIFTY 50" />
        <IndexTicker symbol="NIFTY BANK" />
        <StatCard label="Total Equity" value={summary.loaded ? summary.equity.toLocaleString(undefined, { maximumFractionDigits: 2 }) : "--"} />
        <StatCard
          label="Day P&L"
          value={summary.loaded ? `${summary.dayPnl >= 0 ? "+" : ""}${summary.dayPnl.toFixed(2)}` : "--"}
          tone={summary.loaded ? (summary.dayPnl >= 0 ? "positive" : "negative") : undefined}
        />
        <StatCard
          label="Unrealized P&L"
          value={summary.loaded ? `${summary.unrealizedTotal >= 0 ? "+" : ""}${summary.unrealizedTotal.toFixed(2)}` : "--"}
          tone={summary.loaded ? (summary.unrealizedTotal >= 0 ? "positive" : "negative") : undefined}
        />
        <StatCard
          label={liveAccount ? "Available Margin (Live)" : "Available Cash (Paper)"}
          value={
            liveAccount
              ? liveBalance
                ? liveBalance.available_margin.toLocaleString(undefined, { maximumFractionDigits: 2 })
                : "--"
              : summary.loaded
                ? summary.cash.toLocaleString(undefined, { maximumFractionDigits: 2 })
                : "--"
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[1fr_320px_320px]">
        <Card>
          <CardHeader>
            <CardTitle>{chartInstrument?.symbol ?? "Select an instrument"}</CardTitle>
            <div className="flex gap-1">
              {CHART_TIMEFRAMES.map((tf) => (
                <button
                  key={tf}
                  onClick={() => setTimeframe(tf)}
                  className={`rounded-md px-2 py-1 text-xs font-medium ${
                    timeframe === tf ? "bg-active-soft text-active" : "text-text-muted hover:text-text-secondary"
                  }`}
                >
                  {tf}
                </button>
              ))}
            </div>
          </CardHeader>
          <CardContent>
            {!chartInstrument ? (
              <p className="py-10 text-center text-sm text-text-muted">Pick a symbol from the watchlist to chart it.</p>
            ) : candlesLoading ? (
              <LoadingState />
            ) : (
              <PriceChart candles={candles ?? []} height={420} />
            )}
          </CardContent>
        </Card>

        <WatchlistPanel selectedId={chartInstrument?.id ?? null} onSelect={setSelected} />

        <OrderTicket key={chartInstrument?.id ?? "none"} instrument={chartInstrument} />
      </div>

      <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-[1fr_380px]">
        <PaperPositionsTable />
        <ZerodhaDataCard />
      </div>
    </div>
  );
}
