import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import * as authApi from "@/api/auth";
import type { MeResponse } from "@/api/types";

type AuthState = {
  token: string | null;
  me: MeResponse | null;
  loading: boolean;
  setToken: (token: string | null) => void;
  refreshMe: () => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() =>
    localStorage.getItem("access_token"),
  );
  const [me, setMe] = useState<MeResponse | null>(null);
  const [loading, setLoading] = useState(true);

  const setToken = useCallback((value: string | null) => {
    if (value) localStorage.setItem("access_token", value);
    else localStorage.removeItem("access_token");
    setTokenState(value);
  }, []);

  const logout = useCallback(() => {
    setToken(null);
    setMe(null);
  }, [setToken]);

  const refreshMe = useCallback(async () => {
    if (!localStorage.getItem("access_token")) {
      setMe(null);
      return;
    }
    try {
      const data = await authApi.fetchMe();
      setMe(data);
    } catch {
      logout();
    }
  }, [logout]);

  useEffect(() => {
    (async () => {
      try {
        if (token) await refreshMe();
      } finally {
        setLoading(false);
      }
    })();
  }, [token, refreshMe]);

  const value = useMemo(
    () => ({ token, me, loading, setToken, refreshMe, logout }),
    [token, me, loading, setToken, refreshMe, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
