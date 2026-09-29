# Kissan Saathi — Farmer Memory & Procurement Backend

The backend for **Kissan Saathi**, a farmer MSP procurement platform built for Smart India
Hackathon 2026, Problem Statement 26032. It gives the app and the staff dashboard a single source
of truth for who a farmer is, what they've asked before, and where their slot and payment actually
stand — instead of each screen guessing from local state.

The [app](https://github.com/Priyanshi507/kissan-saathi) (React Native, Expo) and the staff
dashboard (`admin-dashboard.html`, included here) both talk to this backend over HTTP.

## What's in here

- **Conversation memory (RAG)** — three layers (SQL filter → semantic rank → farmer profile) so
  the chat and voice assistant can recall what a farmer said earlier, instead of starting cold
  every time.
- **Procurement** — mandis, slot booking, a live queue position that's computed fresh on every
  read (never goes stale), and payments with a full status timeline.
- **Payment diagnosis** — a fixed, sourced knowledge base of why a payment fails, in English and
  Hindi. The AI only picks which known cause matches what a farmer typed or photographed; it can
  never invent a procedure, a phone number or a website.
- **Grievances** — complaints are built from stored facts only, so nothing in an official
  complaint is ever hallucinated.
- **Notifications & IVR** — an in-app inbox first, SMS and phone calls on top via Twilio
  (optional — everything works without it).
- **Offline-safe by design** — the app queues actions locally before sending, so a booking or
  cancellation made with no signal is never lost.
- **Admin dashboard** — live slots, queue, payments, grievances, farmer registry, and broadcast
  messaging for staff, all reading the same data the app writes.
- **DPDP-compliant erasure** — a single endpoint removes a farmer's chat history and every
  procurement row, with a count of what was deleted.

## Architecture

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env   # then fill in GOOGLE_API_KEY at minimum
export $(cat .env | xargs) && python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
```

Open `admin-dashboard.html` in a browser and sign in with the `ADMIN_TOKEN` from your `.env`.

## Tests

```bash
python3 -m unittest discover tests
```

174 tests: the procurement store (capacity, queue order, learning wait estimates, payment rules),
the payment knowledge base, complaints, notifications (against a fake Twilio transport), IVR
(signature verification, TwiML, answers), and full HTTP journeys through the real routers.

## Demo data

Real data is thin until farmers have actually used the app — an empty Payments tab, for instance,
until someone's been checked in and completed. To see every tab populated at once:

```bash
python3 seed_demo_scenario.py          # loads 8 fictional DEMO- farmers across a 10-day scenario
python3 seed_demo_scenario.py --reset  # removes only the demo rows
```

Demo farmer IDs aren't phone-shaped, so they can never receive a real SMS.

## API and design decisions

The full endpoint list, data model, and the reasoning behind each design choice (why bookings are
idempotent, why queue position is never stored, why the AI never writes payment advice, and more)
are in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Known limits

- No farmer login — the phone number is the identity, as elsewhere in the app. Real deployment
  needs an OTP login first.
- No live payment lookup against an official source; staff record status manually.
- Queue position polls every 15 seconds rather than pushing; push notifications need a full build,
  not Expo Go.
- SQLite, for a prototype and per-farmer access — the storage layer is separate, so moving to
  Postgres later is contained but not done here.
- Payment failure causes come from published explainers, not official NPCI/PFMS documents —
  verify before real deployment.
