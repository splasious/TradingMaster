export interface UserOut {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  roles: string[];
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface BrokerOut {
  id: string;
  code: string;
  name: string;
  is_enabled: boolean;
  is_real_adapter: boolean;
}

export type ConnectionStatus = "connected" | "connecting" | "reconnecting" | "disconnected" | "delayed" | "error";

export interface BrokerAccountOut {
  id: string;
  broker: BrokerOut;
  account_label: string;
  environment: "paper" | "live";
  is_active: boolean;
  connection_status: ConnectionStatus;
}

export interface SystemHealth {
  status: "healthy" | "degraded";
  components: Record<string, "healthy" | "error" | "unreachable">;
}

export const ROLES = ["administrator", "trader", "analyst", "viewer"] as const;
export type Role = (typeof ROLES)[number];

export interface UserCreate {
  email: string;
  password: string;
  full_name: string;
  roles: string[];
}

export const TIMEFRAMES = ["1m", "5m", "15m", "30m", "60m", "1d", "1wk", "1mo"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

export interface InstrumentOut {
  id: string;
  exchange: string;
  symbol: string;
  name: string;
  instrument_type: string;
  data_source: string;
  is_active: boolean;
}

export type BackfillStatus = "pending" | "running" | "completed" | "failed";

export interface BackfillJobOut {
  id: string;
  instrument_id: string;
  timeframe: string;
  status: BackfillStatus;
  downloaded_count: number;
  inserted_count: number;
  duplicate_count: number;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface CandleOut {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface QualityReportOut {
  instrument_id: string;
  timeframe: string;
  candle_count: number;
  invalid_ohlc_count: number;
  non_positive_price_count: number;
  missing_weekday_gaps: number;
  quality_score: number;
}

export interface MarketTick {
  type: "tick";
  instrument_id: string;
  price: number;
  ts: string;
  source: "simulated";
}
