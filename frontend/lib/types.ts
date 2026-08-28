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
  components: Record<string, "healthy" | "error">;
}

export const ROLES = ["administrator", "trader", "analyst", "viewer"] as const;
export type Role = (typeof ROLES)[number];

export interface UserCreate {
  email: string;
  password: string;
  full_name: string;
  roles: string[];
}
