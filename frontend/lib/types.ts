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

export interface IndicatorSpecOut {
  code: string;
  name: string;
  category: string;
  output_fields: string[];
  default_params: Record<string, number>;
}

export interface IndicatorPoint {
  ts: string;
  values: Record<string, number | null>;
}

export type ScanOperator = ">" | "<" | ">=" | "<=" | "==";

export interface ScanCondition {
  field: string;
  operator: ScanOperator;
  value: number;
}

export interface ScanMatch {
  instrument: InstrumentOut;
  values: Record<string, number | null>;
}

export interface ScanResponse {
  matched: ScanMatch[];
  scanned_count: number;
}

export interface SavedScanOut {
  id: string;
  name: string;
  exchange: string | null;
  timeframe: string;
  conditions: ScanCondition[];
}

export type RuleNode = { all: RuleNode[] } | { any: RuleNode[] } | ScanCondition;

export interface PositionSizing {
  type: "fixed_quantity" | "percent_capital";
  value: number;
}

export interface RiskRules {
  stop_loss_pct: number | null;
  take_profit_pct: number | null;
  max_positions: number | null;
  max_daily_loss_pct: number | null;
}

export interface StrategyVersionOut {
  id: string;
  version_number: number;
  timeframe: string;
  instrument_ids: string[];
  parameters: Record<string, number>;
  entry_rules: RuleNode | null;
  exit_rules: RuleNode | null;
  python_code: string | null;
  position_sizing: PositionSizing;
  risk_rules: RiskRules;
  created_at: string;
}

export type StrategyStatus =
  | "draft"
  | "backtested"
  | "optimized"
  | "out_of_sample_tested"
  | "paper_trading"
  | "validated"
  | "approved"
  | "live";

export interface StrategyOut {
  id: string;
  name: string;
  description: string | null;
  code_type: "visual" | "python";
  status: StrategyStatus;
  owner_id: string;
  created_at: string;
  updated_at: string;
  latest_version: StrategyVersionOut | null;
}

export interface StrategyVersionCreate {
  timeframe: string;
  instrument_ids: string[];
  parameters: Record<string, number>;
  entry_rules: RuleNode | null;
  exit_rules: RuleNode | null;
  python_code: string | null;
  position_sizing: PositionSizing;
  risk_rules: Partial<RiskRules>;
}

export interface ValidateResult {
  valid: boolean;
  error: string | null;
  sample_signal: string | null;
}

export type BacktestStatus = "pending" | "running" | "completed" | "failed";

export interface BacktestJobOut {
  id: string;
  strategy_id: string;
  instrument_id: string;
  timeframe: string;
  initial_capital: number;
  status: BacktestStatus;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface BacktestMetrics {
  net_profit: number;
  total_return_pct: number;
  cagr_pct: number;
  max_drawdown_pct: number;
  avg_drawdown_pct: number;
  sharpe_ratio: number;
  sortino_ratio: number;
  profit_factor: number;
  win_rate_pct: number;
  loss_rate_pct: number;
  avg_win: number;
  avg_loss: number;
  expectancy: number;
  num_trades: number;
  avg_holding_period_days: number;
  best_trade: number;
  worst_trade: number;
  max_consecutive_wins: number;
  max_consecutive_losses: number;
  recovery_factor: number;
}

export interface MonteCarloResult {
  simulations: number;
  final_equity_p5: number;
  final_equity_p50: number;
  final_equity_p95: number;
  max_drawdown_pct_p50: number;
  max_drawdown_pct_p95: number;
  probability_of_profit_pct: number;
}

export interface BacktestResultOut {
  metrics: BacktestMetrics;
  out_of_sample_metrics: BacktestMetrics | null;
  monte_carlo: MonteCarloResult | null;
  equity_curve: [string, number][];
}

export interface ParamRangeIn {
  name: string;
  min: number;
  max: number;
  step: number;
}

export interface OptimizationJobOut {
  id: string;
  strategy_id: string;
  instrument_id: string;
  status: BacktestStatus;
  error_message: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface OptimizationRunOut {
  params: Record<string, number>;
  metrics: BacktestMetrics;
}

export interface OptimizationResultOut {
  runs: OptimizationRunOut[];
}

export interface PaperPositionOut {
  instrument_symbol: string;
  quantity: number;
  avg_entry_price: number;
  current_price: number | null;
  unrealized_pnl: number | null;
  opened_at: string;
}

export interface PaperDeploymentOut {
  id: string;
  strategy_id: string;
  strategy_name: string;
  instrument_id: string;
  instrument_symbol: string;
  timeframe: string;
  status: "active" | "stopped";
  last_evaluated_at: string | null;
  created_at: string;
  stopped_at: string | null;
  open_position: PaperPositionOut | null;
}

export interface PaperPortfolioOut {
  cash: number;
  initial_capital: number;
  equity: number;
  unrealized_pnl: number;
  realized_pnl_total: number;
  positions: PaperPositionOut[];
}

export interface PaperOrderOut {
  id: string;
  side: string;
  quantity: number;
  price: number;
  status: string;
  reason: string | null;
  created_at: string;
}

export interface PaperTradeOut {
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
}

export interface PaperEvaluationOut {
  action: string;
  signal: string | null;
  price: number | null;
  reason: string | null;
}

export interface BacktestTradeOut {
  entry_ts: string;
  entry_price: number;
  exit_ts: string;
  exit_price: number;
  quantity: number;
  pnl: number;
  pnl_pct: number;
  exit_reason: string;
}
