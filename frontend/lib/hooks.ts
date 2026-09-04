import { useQueries, useQuery } from "@tanstack/react-query";

import { apiFetch } from "./api";
import type {
  AlertOut,
  AlertSeverity,
  BackfillJobOut,
  BackupOut,
  BacktestJobOut,
  BacktestResultOut,
  BacktestTradeOut,
  BfBackfillJobOut,
  BfSource,
  BfWatchlistItemOut,
  BfWatchlistOut,
  BrokerAccountOut,
  BrokerOut,
  CandleOut,
  CatalogSyncSchedulerStatusOut,
  CompletenessOut,
  IndicatorPoint,
  IndicatorSpecOut,
  InstrumentOut,
  KillSwitchOut,
  LiveDeploymentOut,
  LiveOrderOut,
  LiveSyncStatusOut,
  OptimizationJobOut,
  OptimizationResultOut,
  PaperDeploymentOut,
  PaperOrderOut,
  PaperPortfolioOut,
  PaperTradeOut,
  QualityReportOut,
  QuoteOut,
  ReportSummaryOut,
  SafetyCheckOut,
  SourceStatusOut,
  StrategyOut,
  SystemHealth,
  SystemMonitorOut,
  TimeframeOptionOut,
  TradeRowOut,
  UnreadCountOut,
  UserOut,
} from "./types";

export function useSystemHealth() {
  return useQuery({
    queryKey: ["system-health"],
    queryFn: () => apiFetch<SystemHealth>("/api/v1/system/health"),
    refetchInterval: 15_000,
  });
}

export function useBrokers() {
  return useQuery({
    queryKey: ["brokers"],
    queryFn: () => apiFetch<BrokerOut[]>("/api/v1/brokers"),
  });
}

export function useBrokerAccounts() {
  return useQuery({
    queryKey: ["broker-accounts"],
    queryFn: () => apiFetch<BrokerAccountOut[]>("/api/v1/brokers/accounts"),
  });
}

export function useUsers() {
  return useQuery({
    queryKey: ["users"],
    queryFn: () => apiFetch<UserOut[]>("/api/v1/users"),
  });
}

export function useInstruments(q: string, exchange?: string, limit?: number, enabled = true) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (exchange) params.set("exchange", exchange);
  if (limit) params.set("limit", String(limit));
  return useQuery({
    queryKey: ["instruments", q, exchange, limit],
    queryFn: () => apiFetch<InstrumentOut[]>(`/api/v1/instruments?${params.toString()}`),
    enabled,
  });
}

export function useInstrument(instrumentId: string | null) {
  return useQuery({
    queryKey: ["instrument", instrumentId],
    queryFn: () => apiFetch<InstrumentOut>(`/api/v1/instruments/${instrumentId}`),
    enabled: !!instrumentId,
  });
}

export function useBackfillJobs(instrumentId: string | null) {
  return useQuery({
    queryKey: ["backfill-jobs", instrumentId],
    queryFn: () => apiFetch<BackfillJobOut[]>(`/api/v1/market-data/backfill-jobs?instrument_id=${instrumentId}`),
    enabled: !!instrumentId,
  });
}

export function useBackfillJob(jobId: string | null) {
  return useQuery({
    queryKey: ["backfill-job", jobId],
    queryFn: () => apiFetch<BackfillJobOut>(`/api/v1/market-data/backfill-jobs/${jobId}`),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? 1000 : false;
    },
  });
}

export function useQuality(instrumentId: string | null, timeframe: string) {
  return useQuery({
    queryKey: ["quality", instrumentId, timeframe],
    queryFn: () =>
      apiFetch<QualityReportOut>(`/api/v1/market-data/quality?instrument_id=${instrumentId}&timeframe=${timeframe}`),
    enabled: !!instrumentId,
  });
}

export function useQuotes(instrumentIds: string[]) {
  const sortedIds = [...instrumentIds].sort();
  return useQuery({
    queryKey: ["quotes", sortedIds],
    queryFn: () =>
      apiFetch<QuoteOut[]>("/api/v1/market-data/quotes", {
        method: "POST",
        body: JSON.stringify({ instrument_ids: sortedIds }),
      }),
    enabled: sortedIds.length > 0,
  });
}

export function useCandles(instrumentId: string | null, timeframe: string) {
  return useQuery({
    queryKey: ["candles", instrumentId, timeframe],
    queryFn: () =>
      apiFetch<CandleOut[]>(`/api/v1/market-data/candles?instrument_id=${instrumentId}&timeframe=${timeframe}`),
    enabled: !!instrumentId,
  });
}

const TIMEFRAME_ORDER = ["1m", "5m", "15m", "30m", "60m", "4h", "1d", "1wk", "1mo"];

export function useAvailableTimeframes(instrumentId: string | null) {
  return useQuery({
    queryKey: ["available-timeframes", instrumentId],
    queryFn: () => apiFetch<string[]>(`/api/v1/market-data/candles/available-timeframes?instrument_id=${instrumentId}`),
    enabled: !!instrumentId,
  });
}

/** Picks, from what's actually stored for this instrument, the timeframe
 * to fetch candles/indicators from: the request itself if it's directly
 * stored, else the finest stored timeframe finer than (or equal to) the
 * request, to be resampled server-side. Shared by candles and indicators
 * so both derive from the same base and stay visually consistent. */
export function useResampleBase(instrumentId: string | null, timeframe: string) {
  const { data: available } = useAvailableTimeframes(instrumentId);

  const directlyAvailable = !!available?.includes(timeframe);
  const baseTimeframe = (() => {
    if (directlyAvailable || !available?.length) return null;
    const targetIdx = TIMEFRAME_ORDER.indexOf(timeframe);
    if (targetIdx === -1) return null;
    let best: string | null = null;
    for (const tf of available) {
      const idx = TIMEFRAME_ORDER.indexOf(tf);
      if (idx !== -1 && idx <= targetIdx && (best === null || idx > TIMEFRAME_ORDER.indexOf(best))) best = tf;
    }
    return best;
  })();

  return { available, loaded: available !== undefined, directlyAvailable, baseTimeframe };
}

/** Fetches the requested timeframe directly if it's actually stored;
 * otherwise picks the finest stored timeframe finer than (or equal to)
 * the request and resamples it server-side -- so as soon as any base
 * granularity is backfilled (e.g. 1m), every coarser timeframe on the
 * chart's dropdown just works, no separate backfill per timeframe. */
export function useChartCandles(instrumentId: string | null, timeframe: string) {
  const { loaded, directlyAvailable, baseTimeframe } = useResampleBase(instrumentId, timeframe);

  return useQuery({
    queryKey: ["chart-candles", instrumentId, timeframe, directlyAvailable, baseTimeframe],
    queryFn: () =>
      directlyAvailable
        ? apiFetch<CandleOut[]>(`/api/v1/market-data/candles?instrument_id=${instrumentId}&timeframe=${timeframe}`)
        : apiFetch<CandleOut[]>(
            `/api/v1/market-data/candles/resampled?instrument_id=${instrumentId}&base_timeframe=${baseTimeframe}&target_timeframe=${timeframe}`,
          ),
    enabled: !!instrumentId && loaded && (directlyAvailable || !!baseTimeframe),
  });
}

export function useStrategies() {
  return useQuery({
    queryKey: ["strategies"],
    queryFn: () => apiFetch<StrategyOut[]>("/api/v1/strategies"),
  });
}

export function useStrategy(strategyId: string | null) {
  return useQuery({
    queryKey: ["strategy", strategyId],
    queryFn: () => apiFetch<StrategyOut>(`/api/v1/strategies/${strategyId}`),
    enabled: !!strategyId,
  });
}

export function useBacktestsForStrategy(strategyId: string) {
  return useQuery({
    queryKey: ["backtests-for-strategy", strategyId],
    queryFn: () => apiFetch<BacktestJobOut[]>(`/api/v1/backtests?strategy_id=${strategyId}`),
    refetchInterval: (query) => {
      const stillRunning = query.state.data?.some((j) => j.status === "pending" || j.status === "running");
      return stillRunning ? 3000 : false;
    },
  });
}

export function useBacktestJob(jobId: string | null) {
  return useQuery({
    queryKey: ["backtest-job", jobId],
    queryFn: () => apiFetch<BacktestJobOut>(`/api/v1/backtests/${jobId}`),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? 1000 : false;
    },
  });
}

export function useBacktestResult(jobId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["backtest-result", jobId],
    queryFn: () => apiFetch<BacktestResultOut>(`/api/v1/backtests/${jobId}/result`),
    enabled: !!jobId && enabled,
  });
}

export function useBacktestTrades(jobId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["backtest-trades", jobId],
    queryFn: () => apiFetch<BacktestTradeOut[]>(`/api/v1/backtests/${jobId}/trades`),
    enabled: !!jobId && enabled,
  });
}

export function useOptimizationJob(jobId: string | null) {
  return useQuery({
    queryKey: ["optimization-job", jobId],
    queryFn: () => apiFetch<OptimizationJobOut>(`/api/v1/optimization/${jobId}`),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "pending" || status === "running" ? 1000 : false;
    },
  });
}

export function useOptimizationResult(jobId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["optimization-result", jobId],
    queryFn: () => apiFetch<OptimizationResultOut>(`/api/v1/optimization/${jobId}/result`),
    enabled: !!jobId && enabled,
  });
}

export function usePaperDeployments() {
  return useQuery({
    queryKey: ["paper-deployments"],
    queryFn: () => apiFetch<PaperDeploymentOut[]>("/api/v1/paper-trading/deployments"),
    refetchInterval: 5000,
  });
}

export function usePaperPortfolios() {
  return useQuery({
    queryKey: ["paper-portfolios"],
    queryFn: () => apiFetch<PaperPortfolioOut[]>("/api/v1/paper-trading/portfolios"),
    refetchInterval: 5000,
  });
}

export function usePaperOrders(deploymentId: string | null) {
  return useQuery({
    queryKey: ["paper-orders", deploymentId],
    queryFn: () => apiFetch<PaperOrderOut[]>(`/api/v1/paper-trading/orders?deployment_id=${deploymentId}`),
    enabled: !!deploymentId,
  });
}

export function usePaperTrades(deploymentId: string | null) {
  return useQuery({
    queryKey: ["paper-trades", deploymentId],
    queryFn: () => apiFetch<PaperTradeOut[]>(`/api/v1/paper-trading/trades?deployment_id=${deploymentId}`),
    enabled: !!deploymentId,
  });
}

/** Every closed trade across all of the user's paper deployments, newest
 * first -- for a portfolio-wide Closed Trades view, as opposed to
 * usePaperTrades' single-deployment scope. */
export function useAllPaperTrades() {
  return useQuery({
    queryKey: ["paper-trades", "all"],
    queryFn: () => apiFetch<PaperTradeOut[]>("/api/v1/paper-trading/trades"),
  });
}

export function useLiveDeployments() {
  return useQuery({
    queryKey: ["live-deployments"],
    queryFn: () => apiFetch<LiveDeploymentOut[]>("/api/v1/live-trading/deployments"),
    refetchInterval: 5000,
  });
}

export function useLiveOrders(deploymentId: string | null) {
  return useQuery({
    queryKey: ["live-orders", deploymentId],
    queryFn: () => apiFetch<LiveOrderOut[]>(`/api/v1/live-trading/orders?deployment_id=${deploymentId}`),
    enabled: !!deploymentId,
  });
}

export function useKillSwitch() {
  return useQuery({
    queryKey: ["kill-switch"],
    queryFn: () => apiFetch<KillSwitchOut>("/api/v1/live-trading/kill-switch"),
    refetchInterval: 5000,
  });
}

export function useSafetyCheck(strategyId: string | null, brokerAccountId: string | null) {
  return useQuery({
    queryKey: ["safety-check", strategyId, brokerAccountId],
    queryFn: () => apiFetch<SafetyCheckOut>(`/api/v1/live-trading/safety-check?strategy_id=${strategyId}&broker_account_id=${brokerAccountId}`),
    enabled: !!strategyId && !!brokerAccountId,
  });
}

export function useIndicatorList() {
  return useQuery({
    queryKey: ["indicators"],
    queryFn: () => apiFetch<IndicatorSpecOut[]>("/api/v1/indicators"),
  });
}

export function useIndicator(
  instrumentId: string | null,
  timeframe: string,
  indicator: string | null,
  baseTimeframe?: string | null,
  paramOverrides?: Record<string, number>,
) {
  const params = new URLSearchParams({ instrument_id: instrumentId ?? "", timeframe, indicator: indicator ?? "" });
  if (baseTimeframe && baseTimeframe !== timeframe) params.set("base_timeframe", baseTimeframe);
  if (paramOverrides && Object.keys(paramOverrides).length) params.set("params", JSON.stringify(paramOverrides));
  return useQuery({
    queryKey: ["indicator", instrumentId, timeframe, indicator, baseTimeframe, paramOverrides],
    queryFn: () => apiFetch<IndicatorPoint[]>(`/api/v1/indicators/calculate?${params.toString()}`),
    enabled: !!instrumentId && !!indicator,
  });
}

export function useAlerts(unreadOnly: boolean, severity: AlertSeverity | null) {
  const params = new URLSearchParams();
  if (unreadOnly) params.set("unread_only", "true");
  if (severity) params.set("severity", severity);
  return useQuery({
    queryKey: ["alerts", unreadOnly, severity],
    queryFn: () => apiFetch<AlertOut[]>(`/api/v1/alerts?${params.toString()}`),
    refetchInterval: 15_000,
  });
}

export function useUnreadAlertCount() {
  return useQuery({
    queryKey: ["alerts-unread-count"],
    queryFn: () => apiFetch<UnreadCountOut>("/api/v1/alerts/unread-count"),
    refetchInterval: 15_000,
  });
}

export function useSystemMonitor() {
  return useQuery({
    queryKey: ["system-monitor"],
    queryFn: () => apiFetch<SystemMonitorOut>("/api/v1/system/monitor"),
    refetchInterval: 5000,
  });
}

export function useReportSummary(environment: "paper" | "live" | null, start: string | null, end: string | null) {
  const params = new URLSearchParams();
  if (environment) params.set("environment", environment);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  return useQuery({
    queryKey: ["report-summary", environment, start, end],
    queryFn: () => apiFetch<ReportSummaryOut>(`/api/v1/reports/summary?${params.toString()}`),
  });
}

export function useTrades(environment: "paper" | "live" | null, start: string | null, end: string | null) {
  const params = new URLSearchParams();
  if (environment) params.set("environment", environment);
  if (start) params.set("start", start);
  if (end) params.set("end", end);
  return useQuery({
    queryKey: ["report-trades", environment, start, end],
    queryFn: () => apiFetch<TradeRowOut[]>(`/api/v1/reports/trades?${params.toString()}`),
  });
}

export function useBackups() {
  return useQuery({
    queryKey: ["backups"],
    queryFn: () => apiFetch<BackupOut[]>("/api/v1/backup"),
  });
}

export function useBfSourceStatus(source: BfSource) {
  return useQuery({
    queryKey: ["bf-source-status", source],
    queryFn: () => apiFetch<SourceStatusOut>(`/api/v1/backfill-platform/sources/${source}/status`),
    refetchInterval: 30_000,
  });
}

export function useBfJobs(source?: BfSource) {
  const params = source ? `?source=${source}` : "";
  return useQuery({
    queryKey: ["bf-jobs", source ?? "all"],
    queryFn: () => apiFetch<BfBackfillJobOut[]>(`/api/v1/backfill-platform/jobs${params}`),
    refetchInterval: (query) => (query.state.data?.some((j) => j.status === "pending" || j.status === "running") ? 2000 : 10_000),
  });
}

export function useBfCompleteness(source: BfSource, symbol: string | null, timeframe: string, start: string, end: string) {
  return useQuery({
    queryKey: ["bf-completeness", source, symbol, timeframe, start, end],
    queryFn: () =>
      apiFetch<CompletenessOut>(
        `/api/v1/backfill-platform/completeness?source=${source}&symbol=${encodeURIComponent(symbol!)}&timeframe=${timeframe}&start=${start}&end=${end}`,
      ),
    enabled: !!symbol,
  });
}

export function useBfWatchlists() {
  return useQuery({
    queryKey: ["bf-watchlists"],
    queryFn: () => apiFetch<BfWatchlistOut[]>("/api/v1/backfill-platform/watchlists"),
  });
}

export function useBfWatchlistItems(watchlistId: string | null) {
  return useQuery({
    queryKey: ["bf-watchlist-items", watchlistId],
    queryFn: () => apiFetch<BfWatchlistItemOut[]>(`/api/v1/backfill-platform/watchlists/${watchlistId}/items`),
    enabled: !!watchlistId,
  });
}

// Short badge labels for the curated Delta token watchlists, keyed by their
// exact Data Backfill Platform watchlist name -- the real, current source
// of Delta category membership (replaced the older static BTC/ETH/SOL
// prefix guess, which matched almost nothing since most Delta instruments
// are xStock/bStock tokens, not crypto pairs).
const DELTA_CATEGORY_LABELS: Record<string, string> = {
  "Delta Metals (Gold/Silver Tokens)": "Metals",
  "Delta DeFi Tokens": "DeFi",
  "Delta Meme Tokens": "Meme",
  "Delta Smart Contract Platforms": "Smart Contract",
  "Delta US Stocks (xStock/bStocks Tokens)": "US Stocks",
};

// One source of truth for the category filter dropdowns on Markets/Charts,
// so they can't drift from the labels useDeltaCategoryMap actually assigns.
export const DELTA_CATEGORY_OPTIONS = Object.values(DELTA_CATEGORY_LABELS);

/** Symbol -> short category label (e.g. "BTCUSD" -> "Metals"), derived from
 * live membership of the 4 curated Delta watchlists. Undefined while still
 * loading; a symbol with no entry belongs to none of the curated lists. */
export function useDeltaCategoryMap(): Map<string, string> | undefined {
  const { data: watchlists } = useBfWatchlists();
  const categoryWatchlists = (watchlists ?? []).filter((w) => w.name in DELTA_CATEGORY_LABELS);

  const itemQueries = useQueries({
    queries: categoryWatchlists.map((w) => ({
      queryKey: ["bf-watchlist-items", w.id],
      queryFn: () => apiFetch<BfWatchlistItemOut[]>(`/api/v1/backfill-platform/watchlists/${w.id}/items`),
    })),
  });

  if (!watchlists) return undefined;

  const map = new Map<string, string>();
  categoryWatchlists.forEach((w, idx) => {
    const label = DELTA_CATEGORY_LABELS[w.name];
    for (const item of itemQueries[idx]?.data ?? []) {
      if (item.source === "delta") map.set(item.symbol, label);
    }
  });
  return map;
}

export function useBfTimeframes(source: BfSource) {
  return useQuery({
    queryKey: ["bf-timeframes", source],
    queryFn: () => apiFetch<TimeframeOptionOut[]>(`/api/v1/backfill-platform/sources/${source}/timeframes`),
  });
}

export function useBfLiveSyncStatus() {
  return useQuery({
    queryKey: ["bf-live-sync-status"],
    queryFn: () => apiFetch<LiveSyncStatusOut>("/api/v1/backfill-platform/live-sync/status"),
    refetchInterval: 30_000,
  });
}

export function useCatalogSyncStatus() {
  return useQuery({
    queryKey: ["catalog-sync-status"],
    queryFn: () => apiFetch<CatalogSyncSchedulerStatusOut>("/api/v1/backfill-platform/catalog-sync/status"),
    refetchInterval: 30_000,
  });
}
