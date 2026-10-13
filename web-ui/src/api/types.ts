export type SubscriptionInfo = {
  status: string;
  is_trial: boolean;
  expire_at: string;
  url: string;
  plan_name: string;
  plan_duration_days: number;
  used_traffic_bytes?: number | null;
  lifetime_used_traffic_bytes?: number | null;
  online_at?: string | null;
};

export type Device = {
  hwid: string;
  platform?: string | null;
  device_model?: string | null;
  os_version?: string | null;
  user_agent?: string | null;
};

export type PlanOffer = {
  code: string;
  name: string;
  description?: string | null;
  durations: Array<{
    days: number;
    prices: Array<{ gateway: string; amount: number; currency: string }>;
  }>;
};

export type TokenResponse = {
  access_token: string;
  token_type?: string;
};

export type MeResponse = {
  id: number;
  email?: string | null;
  username?: string | null;
  name?: string | null;
  is_email_verified?: boolean;
};
