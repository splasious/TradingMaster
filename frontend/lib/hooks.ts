import { useQuery } from "@tanstack/react-query";

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
  ReportSummaryOut,
  SafetyCheckOut,
  SourceStatusOut,
  StrategyOut,
  SystemHealth,
  SystemMonitorOut,
  TimeframeOptionOut,
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

export function useInstruments(q: string, exchange?: string) {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (exchange) params.set("exchange", exchange);
  return useQuery({
    queryKey: ["instruments", q, exchange],
    queryFn: () => apiFetch<InstrumentOut[]>(`/api/v1/instruments?${params.toString()}`),
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

export function useCandles(instrumentId: string | null, timeframe: string) {
  return useQuery({
    queryKey: ["candles", instrumentId, timeframe],
    queryFn: () =>
      apiFetch<CandleOut[]>(`/api/v1/market-data/candles?instrument_id=${instrumentId}&timeframe=${timeframe}`),
    enabled: !!instrumentId,
  });
}

export function useChartCandles(instrumentId: string | null, timeframe: string) {
  const resampled = timeframe === "1wk" || timeframe === "1mo";
  return useQuery({
    queryKey: ["chart-candles", instrumentId, timeframe],
    queryFn: () =>
      resampled
        ? apiFetch<CandleOut[]>(
            `/api/v1/market-data/candles/resampled?instrument_id=${instrumentId}&base_timeframe=1d&target_timeframe=${timeframe}`,
          )
        : apiFetch<CandleOut[]>(`/api/v1/market-data/candles?instrument_id=${instrumentId}&timeframe=${timeframe}`),
    enabled: !!instrumentId,
  });
}

export function useStrategies() {
  return useQuery({
    queryKey: ["strategies"],
    queryFn: () => apiFetch<StrategyOut[]>("/api/v1/strategies"),
  });
}

export function useBacktestsForStrategy(strategyId: string) {
  return useQuery({
    queryKey: ["backtests-for-strategy", strategyId],
    queryFn: () => apiFetch<BacktestJobOut[]>(`/api/v1/backtests?strategy_id=${strategyId}`),
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

export function usePaperPortfolio() {
  return useQuery({
    queryKey: ["paper-portfolio"],
    queryFn: () => apiFetch<PaperPortfolioOut>("/api/v1/paper-trading/portfolio"),
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

export function useIndicator(instrumentId: string | null, timeframe: string, indicator: string | null) {
  return useQuery({
    queryKey: ["indicator", instrumentId, timeframe, indicator],
    queryFn: () =>
      apiFetch<IndicatorPoint[]>(
        `/api/v1/indicators/calculate?instrument_id=${instrumentId}&timeframe=${timeframe}&indicator=${indicator}`,
      ),
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
