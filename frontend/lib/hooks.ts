import { useQuery } from "@tanstack/react-query";

import { apiFetch } from "./api";
import type { BrokerAccountOut, BrokerOut, SystemHealth, UserOut } from "./types";

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
