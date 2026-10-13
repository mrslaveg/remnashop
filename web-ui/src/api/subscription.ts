import { api } from "./client";
import type { Device, PlanOffer, SubscriptionInfo } from "./types";

export function getCurrentSubscription() {
  return api<SubscriptionInfo | null>("/api/public/subscription/current");
}

export function getDevices() {
  return api<{ devices: Device[] }>("/api/public/subscription/devices");
}

export function deleteDevice(hwid: string) {
  return api<{ remaining?: number }>(
    `/api/public/subscription/devices/${encodeURIComponent(hwid)}`,
    { method: "DELETE" },
  );
}

export function getPlans() {
  return api<PlanOffer[] | { plans: PlanOffer[] }>("/api/public/plans");
}
