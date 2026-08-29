import { AlertTriangle, CheckCircle2, WifiOff } from "lucide-react";

import { Badge, type Tone } from "@/components/ui/badge";

export type DataStatus = "live" | "stale" | "disconnected";

interface MarketContextBarProps {
  broker: string;
  market: string;
  instrument: string;
  instrumentType?: string;
  timeframe?: string;
  mode?: "Paper" | "Live";
  dataStatus?: DataStatus;
}

const DATA_STATUS_CONFIG: Record<DataStatus, { label: string; tone: Tone; Icon: typeof CheckCircle2 }> = {
  live: { label: "Live", tone: "positive", Icon: CheckCircle2 },
  stale: { label: "Stale", tone: "warning", Icon: AlertTriangle },
  disconnected: { label: "Disconnected", tone: "inactive", Icon: WifiOff },
};

function Field({ label, value }: { label: string; value: string }) {
  return (
    <span className="text-xs text-text-secondary">
      <span className="text-text-muted">{label}: </span>
      <span className="font-medium text-text-primary">{value}</span>
    </span>
  );
}

/** Every major trading screen shows Broker / Market / Instrument /
 * Instrument Type / Timeframe / Trading Mode / Data Status together
 * (UI/UX spec section 4) so a user never has to infer trading context
 * from scattered page chrome. Risk Status is deliberately omitted here --
 * no real per-deployment risk metric exists yet, and fabricating one
 * would violate "never invent data the system doesn't actually have". */
export function MarketContextBar({ broker, market, instrument, instrumentType, timeframe, mode, dataStatus }: MarketContextBarProps) {
  const status = dataStatus ? DATA_STATUS_CONFIG[dataStatus] : null;

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-md border border-border bg-surface-elevated/50 px-3 py-2">
      <Field label="Broker" value={broker} />
      <Field label="Market" value={market} />
      <Field label="Instrument" value={instrument} />
      {instrumentType && <Field label="Type" value={instrumentType} />}
      {timeframe && <Field label="Timeframe" value={timeframe} />}
      {mode && <Badge tone={mode === "Live" ? "critical" : "warning"}>{mode}</Badge>}
      {status && (
        <Badge tone={status.tone}>
          <status.Icon className="h-3 w-3" />
          {status.label}
        </Badge>
      )}
    </div>
  );
}
