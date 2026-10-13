import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/api/client";
import * as subApi from "@/api/subscription";
import type { Device } from "@/api/types";

export function DevicesPage() {
  const [devices, setDevices] = useState<Device[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await subApi.getDevices();
      setDevices(res.devices || []);
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Failed to load devices");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onDelete(hwid: string) {
    setBusy(hwid);
    try {
      await subApi.deleteDevice(hwid);
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "Delete failed");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <p className="error-text">{error}</p>;

  return (
    <div className="stack">
      <h2 style={{ margin: 0 }}>Устройства</h2>
      {!devices.length && <p className="muted">Нет привязанных устройств</p>}
      {devices.map((d) => (
        <div key={d.hwid} className="card row" style={{ justifyContent: "space-between" }}>
          <div className="stack" style={{ gap: "0.25rem" }}>
            <strong>{d.device_model || d.platform || "Device"}</strong>
            <span className="muted">{d.hwid}</span>
            <span className="muted">
              {[d.platform, d.os_version].filter(Boolean).join(" · ")}
            </span>
          </div>
          <button
            type="button"
            className="danger"
            disabled={busy === d.hwid}
            onClick={() => onDelete(d.hwid)}
          >
            Удалить
          </button>
        </div>
      ))}
    </div>
  );
}
