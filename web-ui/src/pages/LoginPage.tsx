import { FormEvent, useEffect, useState } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import * as authApi from "@/api/auth";
import { ApiError } from "@/api/client";
import { useAuth } from "@/auth/AuthContext";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

export function LoginPage() {
  const { token, setToken } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const botUsername = import.meta.env.VITE_TELEGRAM_BOT_USERNAME || "";

  useEffect(() => {
    if (!botUsername) return;

    window.onTelegramAuth = async (user) => {
      setError(null);
      setBusy(true);
      try {
        const res = await authApi.loginTelegram(user);
        setToken(res.access_token);
        navigate("/", { replace: true });
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : "Telegram login failed");
      } finally {
        setBusy(false);
      }
    };

    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", botUsername);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "10");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    script.setAttribute("data-request-access", "write");
    const mount = document.getElementById("tg-login");
    mount?.replaceChildren(script);

    return () => {
      delete window.onTelegramAuth;
    };
  }, [botUsername, navigate, setToken]);

  if (token) return <Navigate to="/" replace />;

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res =
        mode === "login"
          ? await authApi.loginEmail(email, password)
          : await authApi.registerEmail({ email, password, name: name || undefined });
      setToken(res.access_token);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Request failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <div className="card login-card stack">
        <h1>Кабинет</h1>
        <p className="muted">Войдите по email или через Telegram</p>

        <div className="row">
          <button
            type="button"
            className={mode === "login" ? "" : "secondary"}
            onClick={() => setMode("login")}
          >
            Вход
          </button>
          <button
            type="button"
            className={mode === "register" ? "" : "secondary"}
            onClick={() => setMode("register")}
          >
            Регистрация
          </button>
        </div>

        <form className="stack" onSubmit={onSubmit}>
          {mode === "register" && (
            <input
              placeholder="Имя"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="name"
            />
          )}
          <input
            type="email"
            placeholder="Email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            autoComplete="email"
          />
          <input
            type="password"
            placeholder="Пароль"
            required
            minLength={6}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
          />
          {error && <div className="error-text">{error}</div>}
          <button type="submit" disabled={busy}>
            {busy ? "…" : mode === "login" ? "Войти" : "Создать аккаунт"}
          </button>
        </form>

        <div className="divider muted">или</div>
        {botUsername ? (
          <div id="tg-login" />
        ) : (
          <p className="muted">
            Укажите <code>VITE_TELEGRAM_BOT_USERNAME</code> для Telegram Login.
          </p>
        )}
      </div>
      <style>{`
        .login-wrap {
          min-height: 100vh; display: grid; place-items: center; padding: 1rem;
        }
        .login-card { width: min(420px, 100%); }
        .login-card h1 { margin: 0; font-size: 1.5rem; }
        .divider { text-align: center; margin: 0.25rem 0; }
      `}</style>
    </div>
  );
}
