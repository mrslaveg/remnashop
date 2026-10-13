# Remnashop Web Cabinet

React + Vite + TypeScript UI for the Remnashop public API.

## Dev

```bash
cp .env.example .env
# set VITE_TELEGRAM_BOT_USERNAME if using Telegram Login
npm install
npm run dev
```

API calls go to `/api/...`. In dev, Vite proxies `/api` to `VITE_API_PROXY` (default `http://127.0.0.1:8000`).

Ensure Remnashop has `web_enabled=true` and CORS / origins include the UI origin.

## Build

```bash
npm run build
# static files in dist/
```

## Screens

- `/login` — email + Telegram
- `/` — current subscription
- `/plans` — plans / purchase
- `/devices` — HWID devices
