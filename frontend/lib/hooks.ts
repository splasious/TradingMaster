import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./api";
import type {
  BackfillJobOut,
  BacktestJobOut,
  BacktestResultOut,
  BacktestTradeOut,
  BrokerAccountOut,
  BrokerOut,
  CandleOut,
  IndicatorPoint,
  IndicatorSpecOut,
  InstrumentOut,
  OptimizationJobOut,
  OptimizationResultOut,
  PaperDeploymentOut,
  PaperOrderOut,
  PaperPortfolioOut,
  PaperTradeOut,
  QualityReportOut,
  StrategyOut,
  SystemHealth,
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
