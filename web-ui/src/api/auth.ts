import { api } from "./client";
import type { MeResponse, TokenResponse } from "./types";

export function loginEmail(email: string, password: string) {
  return api<TokenResponse>("/api/public/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function registerEmail(payload: {
  email: string;
  password: string;
  name?: string;
}) {
  return api<TokenResponse>("/api/public/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function loginTelegram(payload: Record<string, unknown>) {
  return api<TokenResponse>("/api/public/auth/telegram", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchMe() {
  return api<MeResponse>("/api/public/auth/me");
}
