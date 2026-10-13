import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "@/api/client";
import * as subApi from "@/api/subscription";
import type { SubscriptionInfo } from "@/api/types";

function formatBytes(value?: number | null) {
  if (value == null) return "—";
  const gb = value / (1024 ** 3);
  return `${gb.toFixed(2)} GB`;
}

export function SubscriptionPage() {
  const [data, setData] = useState<SubscriptionInfo | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        setData(await subApi.getCurrentSubscription());
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : "Failed to load subscription");
        setData(null);
      }
    })();
  }, []);

  if (data === undefined) return <p className="muted">Загрузка подписки…</p>;
  if (error) return <p className="error-text">{error}</p>;
  if (!data) {
    return (
      <div className="card stack">
        <h2>Нет активной подписки</h2>
        <p className="muted">Выберите тариф, чтобы начать.</p>
        <Link to="/plans">
          <button type="button">К тарифам</button>
        </Link>
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="card stack">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>{data.plan_name}</h2>
          <span className="muted">{data.status}{data.is_trial ? " · trial" : ""}</span>
        </div>
        <div className="muted">Истекает: {new Date(data.expire_at).toLocaleString()}</div>
        <div className="muted">
          Трафик: {formatBytes(data.used_traffic_bytes)} / lifetime{" "}
          {formatBytes(data.lifetime_used_traffic_bytes)}
        </div>
        {data.online_at && (
          <div className="muted">Онлайн: {new Date(data.online_at).toLocaleString()}</div>
        )}
        <div className="stack">
          <label className="muted">Ссылка подписки</label>
          <input readOnly value={data.url} onFocus={(e) => e.target.select()} />
          <div className="row">
            <button
              type="button"
              onClick={() => navigator.clipboard.writeText(data.url)}
            >
              Копировать
            </button>
            <a href={data.url} target="_blank" rel="noreferrer">
              <button type="button" className="secondary">
                Открыть
              </button>
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
