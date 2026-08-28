"use client";

import { useEffect, useRef, useState } from "react";

import { getAccessToken } from "./api";
import type { ConnectionStatus, MarketTick } from "./types";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const WS_BASE_URL = API_BASE_URL.replace(/^http/, "ws");

const RECONNECT_DELAY_MS = 2000;

interface PriceState {
  price: number;
  ts: string;
}

interface MarketDataSocketState {
  status: ConnectionStatus;
  prices: Record<string, PriceState>;
  latencyMs: number | null;
}

/** Subscribes to simulated live ticks for a set of instrument ids over the
 * market-data WebSocket (PRD section 9's connection lifecycle: connecting /
 * connected / reconnecting / disconnected / error, with heartbeat-derived
 * latency). Every price this returns is simulated -- see tick_engine.py. */
export function useMarketDataSocket(instrumentIds: string[]): MarketDataSocketState {
  const [status, setStatus] = useState<ConnectionStatus>("connecting");
  const [prices, setPrices] = useState<Record<string, PriceState>>({});
  const [latencyMs, setLatencyMs] = useState<number | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const subscribedRef = useRef<Set<string>>(new Set());
  const idsRef = useRef<string[]>(instrumentIds);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUsRef = useRef(false);

  useEffect(() => {
    closedByUsRef.current = false;

    function connect() {
      const token = getAccessToken();
      if (!token) {
        // Access token not ready yet (e.g. right after a page refresh, before
        // the silent auth refresh resolves) -- retry shortly rather than
        // opening a socket the server will reject.
        reconnectTimerRef.current = setTimeout(connect, 500);
        return;
      }

      setStatus((prev) => (prev === "connected" ? "reconnecting" : "connecting"));
      const ws = new WebSocket(`${WS_BASE_URL}/api/v1/ws/market-data?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        setStatus("connected");
        subscribedRef.current = new Set(idsRef.current);
        if (idsRef.current.length) {
          ws.send(JSON.stringify({ type: "subscribe", instrument_ids: idsRef.current }));
        }
      };

      ws.onmessage = (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "tick") {
          const tick = message as MarketTick;
          setPrices((prev) => ({ ...prev, [tick.instrument_id]: { price: tick.price, ts: tick.ts } }));
        } else if (message.type === "heartbeat") {
          setLatencyMs(Math.max(0, Date.now() - new Date(message.ts).getTime()));
        }
      };

      ws.onclose = () => {
        if (closedByUsRef.current) return;
        setStatus("reconnecting");
        reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
      };

      ws.onerror = () => {
        setStatus("error");
      };
    }

    connect();

    return () => {
      closedByUsRef.current = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  useEffect(() => {
    idsRef.current = instrumentIds;

    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const current = new Set(instrumentIds);
    const toSubscribe = instrumentIds.filter((id) => !subscribedRef.current.has(id));
    const toUnsubscribe = [...subscribedRef.current].filter((id) => !current.has(id));

    if (toSubscribe.length) ws.send(JSON.stringify({ type: "subscribe", instrument_ids: toSubscribe }));
    if (toUnsubscribe.length) ws.send(JSON.stringify({ type: "unsubscribe", instrument_ids: toUnsubscribe }));
    subscribedRef.current = current;
  }, [instrumentIds]);

  return { status, prices, latencyMs };
}
