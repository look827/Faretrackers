# FareTracker

A full-stack starter for monitoring flight fares, recording price history, and sending change/target-price alerts.

## Stack
- React + Vite frontend
- FastAPI backend
- SQLite by default (easy local start); can be changed to PostgreSQL with `DATABASE_URL`
- Duffel API integration (requires a valid Duffel token)
- In-site notifications and optional browser notifications; email alerts via SMTP

## Run locally

### 1) Backend
```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env   # Windows PowerShell: Copy-Item .env.example .env
# macOS/Linux: cp .env.example .env
uvicorn app:app --reload --port 8000
```
Edit `backend/.env`. Set `DUFFEL_ACCESS_TOKEN` to a valid token to enable live searches. Without it, the app clearly marks fare results as demo data and does not pretend they are live.

### 2) Frontend
In another terminal:
```bash
cd frontend
npm install
npm run dev
```
Open the local URL printed by Vite. The backend must be running on port 8000.

## Features
- Add/remove route watches
- Search now and persist observations
- Price-change and target-price alerts
- Price history chart
- In-app alert feed
- Optional browser notification permission
- Optional SMTP email alerts

## Important limitations
- A deployed backend is needed for checks to continue while your computer is off.
- Configure a scheduled job / worker on your host for automatic recurring checks. This starter provides a `POST /api/check-all` endpoint for that job.
- Duffel inventory and prices depend on the provider and airline coverage; verify fares before booking.
- Never put API keys in frontend code. Keep them in backend environment variables.
- This is a fare monitor, not an automatic booking service.
