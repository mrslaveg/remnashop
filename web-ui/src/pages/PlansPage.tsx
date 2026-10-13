import { useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import * as subApi from "@/api/subscription";
import type { PlanOffer } from "@/api/types";

export function PlansPage() {
  const [plans, setPlans] = useState<PlanOffer[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const res = await subApi.getPlans();
        setPlans(Array.isArray(res) ? res : res.plans || []);
      } catch (e) {
        setError(e instanceof ApiError ? e.detail : "Failed to load plans");
      }
    })();
  }, []);

  if (error) return <p className="error-text">{error}</p>;
  if (!plans.length) return <p className="muted">Загрузка тарифов…</p>;

  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>Тарифы</h2>
      <p className="muted">
        Оплата через web-gateway будет подключена следующим шагом (endpoint purchase уже есть в API).
      </p>
      {plans.map((plan) => (
        <div key={plan.code} className="card stack">
          <strong>{plan.name}</strong>
          {plan.description && <span className="muted">{plan.description}</span>}
          <ul className="muted">
            {plan.durations?.map((d) => (
              <li key={d.days}>
                {d.days} дн. —{" "}
                {d.prices?.map((p) => `${p.amount} ${p.currency} (${p.gateway})`).join(", ")}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
