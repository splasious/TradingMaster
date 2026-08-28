import { AlertOctagon, CheckCircle2, CircleOff, Loader2, WifiOff } from "lucide-react";

import type { ConnectionStatus } from "@/lib/types";

import { Badge, type Tone } from "./badge";

const STATUS_CONFIG: Record<ConnectionStatus, { label: string; tone: Tone; Icon: typeof CheckCircle2 }> = {
  connected: { label: "Connected", tone: "positive", Icon: CheckCircle2 },
  connecting: { label: "Connecting", tone: "active", Icon: Loader2 },
  reconnecting: { label: "Reconnecting", tone: "warning", Icon: Loader2 },
  delayed: { label: "Delayed", tone: "warning", Icon: AlertOctagon },
  disconnected: { label: "Disconnected", tone: "inactive", Icon: CircleOff },
  error: { label: "Error", tone: "critical", Icon: WifiOff },
};

/** Status is always shown as color + icon + label together (PRD 39.3: never
 * communicate state through color alone). */
export function ConnectionStatusBadge({ status }: { status: ConnectionStatus }) {
  const { label, tone, Icon } = STATUS_CONFIG[status];
  const spinning = status === "connecting" || status === "reconnecting";
  return (
    <Badge tone={tone}>
      <Icon className={spinning ? "h-3 w-3 animate-spin" : "h-3 w-3"} />
      {label}
    </Badge>
  );
}
