import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./api";
import type {
  BackfillJobOut,
  BrokerAccountOut,
  BrokerOut,
  CandleOut,
  InstrumentOut,
  QualityReportOut,
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
