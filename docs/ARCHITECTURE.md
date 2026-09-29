# Kissan Saathi backend: architecture

This document explains how the backend fits together, why it is built this way, how to run and
test it, and what it deliberately does not do.

## 1. The idea in one picture

```
 Farmer app (React Native) ─┐                         ┌─ Staff dashboard (admin-dashboard.html)
 Farmer phone call (Twilio) ─┼─►  FastAPI  (api.py)  ◄─┘
 SMS out (Twilio, optional) ◄┘        │
                                      ├─ routes/farmer_routes.py   slots, payments, diagnosis, complaints, inbox
                                      ├─ routes/staff_routes.py    check-in, complete, record payment, message log
                                      ├─ routes/ivr_routes.py      Twilio voice webhooks
                                      │
                       ┌──────────────┴───────────────┐
                       ▼                              ▼
              procurement/  (this round)      farmer_memory/  (earlier rounds)
              slots · queue · payments        conversation memory (RAG) · grievances
              notifications · IVR helpers     three layers: SQL filter → semantic rank → profile
                       └──────────────┬───────────────┘
                                      ▼
                              one SQLite file: kissan_saathi.db
```

Before this round, slots, payments and mandis existed only as local state inside the app, so the
dashboard could only show sample data and no queue position could exist. The `procurement` package
is the single source of truth that fixes that. Everything else is a thin layer on top of it.

## 2. Modules

| Module | Responsibility |
|---|---|
| `procurement/store.py` | Tables and rules for mandis, slots, the live queue, payments (with timeline), notifications, language preference. |
| `procurement/payment_kb.py` | Fixed, sourced list of why a payment fails, with English and Hindi advice. Keyword classifier and AI-classifier prompt/parser. |
| `procurement/complaint.py` | Builds a complaint from stored facts only. Status labels. |
| `procurement/notify.py` | Message templates (en/hi), in-app inbox writes, SMS adapter (`LogSms` or `TwilioSms`). |
| `procurement/ivr.py` | Twilio signature check, TwiML builders, and answers to payment/queue questions straight from the database. |
| `routes/*.py` | HTTP endpoints. Each `build(deps)` receives its dependencies, so tests use in-memory stores and no global state. |
| `api.py` | Creates the stores, wires the routers, and keeps the earlier chat, voice, image and admin endpoints. |

## 3. Design decisions, and why

**Bookings are idempotent.** Every booking carries a `client_id` made by the app. If the phone is
offline and the same booking is replayed twice, the server still creates one slot. Without this,
offline mode would create duplicate slots.

**Queue position is computed, never stored.** Position is worked out from the current bookings each
time it is read, so it cannot go stale when someone cancels or checks in. Farmers who have arrived are
served before those who have not, in arrival order; the rest follow booking order.

**The wait estimate learns, and says when it is guessing.** It uses the real check-in to completion
times at that mandi. Until at least one plausible sample exists (one minute or more, to ignore
batch-closed slots) it assumes 6 minutes per farmer and labels that as assumed.

**The AI never writes payment advice.** `payment_kb.py` holds a fixed list of causes and fixes. The AI's
only job is to pick which known cause matches what the farmer typed or photographed. It cannot invent a
procedure, a phone number or a website. Order of trust: the centre's own recorded reason, then the
farmer's words (keyword match, no AI needed), then the AI reading a photo, then a default based on status.

**Complaints contain only stored facts.** `complaint.py` fills a template from the payment record and
its timeline. Nothing is generated, so nothing can be hallucinated into an official complaint.

**Payment and queue questions on the phone are answered from the database.** A wrong number spoken
aloud is worse than a slow answer, so only other questions go to the model.

**Every event goes to an in-app inbox first.** SMS is optional on top. Without Twilio, messages are
recorded and shown as "Logged only" in the dashboard, and never claimed as sent.

**Offline mode saves an action before sending it.** The app writes each action to a local outbox, then
sends it. Closing the app mid-request cannot lose it. Failed sends retry with growing delays (5 s, 10 s,
20 s, then every 30 s at most), immediately whenever the app opens or returns to the foreground, and one
successful send releases everything else that is waiting. Real client errors (for example "that time is full") are final and are
not retried; the server leaves an explanation in the farmer's inbox.

## 4. Data model (SQLite)

| Table | Purpose |
|---|---|
| `interactions`, `farmer_profiles` | Conversation memory and grievances (earlier rounds). |
| `mandis` | id, name, coordinates, capacity per window (default 20). Filled when the first booking arrives. |
| `slots` | One row per booking: `client_id` (unique), farmer, mandi, date, window, status, timestamps. |
| `payments` | One row per sale: amount, status, failure code, linked complaint. |
| `payment_events` | The timeline shown to the farmer and used in the complaint. |
| `notifications` | The inbox, plus `sms_status` (`none`, `pending`, `logged`, `sent`, `failed`, `no_phone`). |
| `farmer_prefs` | The farmer's language, so messages arrive in the right one. |
| `farmer_registry` | Who has registered in the app (name, crops, district, state). The app sends this at every launch, so the dashboard lists farmers who have never chatted. |

Slot statuses: `booked` → `checked_in` → `completed`, or `no_show` / `cancelled`. Payment statuses:
`pending`, `processing`, `paid`, `failed`. Completing a slot automatically opens a `pending` payment.

## 5. API summary

Farmer-facing (no admin token):

| Method and path | Purpose |
|---|---|
| `POST /slots` | Book (idempotent on `client_id`). `409 window_full` if the time is full. |
| `POST /slots/{client_id}/cancel` | Cancel. |
| `GET /farmer/{id}/slots` | Slots with live queue position and wait estimate. |
| `GET /farmer/{id}/payments` | Payments with timeline, days waiting, `can_escalate`, complaint status. |
| `POST /farmer/{id}/payments/{pid}/diagnose` | Explain why a payment is stuck. Optional `text`, `image_base64`, `language`. |
| `POST /farmer/{id}/payments/{pid}/escalate` | Register a complaint. Idempotent. `409 not_yet` if too early. |
| `GET /farmer/{id}/grievances` | Complaint status and officer notes. |
| `GET /farmer/{id}/notifications`, `POST .../read` | Inbox. |
| `PUT /farmer/{id}/prefs` | Save language. |
| `PUT /farmer/{id}/profile` | Save name, crops, district and state (safe to repeat). Feeds the dashboard's Farmer Registry. |
| `DELETE /farmer/{id}` | Full DPDP erasure: chat history and every procurement row (slots, payments, notifications, prefs, registry). Returns a count of what was removed. |

Staff (header `X-Admin-Token`): `GET /admin/slots`, `PATCH /admin/slots/{id}` (check-in, complete with
optional amount, no-show, cancel), `GET /admin/queue`, `GET/POST /admin/payments`,
`PATCH /admin/payments/{id}`, `GET /admin/notifications`, `GET /admin/failure-codes`, `GET /admin/farmers` (registered and chatted farmers merged, with location, language and slot/payment counts), `GET /admin/farmers/{id}` (profile plus full slots/payments/complaints/chat history), `GET /admin/grievances` (each with the farmer's name and its linked payment), `PATCH /admin/grievances/{id}` (also notifies the farmer), `GET /admin/activity` (one merged, newest-first feed of registrations, chats, complaints, slot and payment events, for the Overview timeline), `GET /admin/procurement-trends` (daily booked/completed counts and payment-status/amount breakdown, for the Overview charts), `GET /admin/mandis` and `PATCH /admin/mandis/{id}` (view and edit a mandi's per-window capacity; takes effect on the next booking), `POST /admin/announce` (send a free-text message to one farmer or broadcast to all — title ≤80 characters, body ≤500).

Phone (Twilio): `POST /ivr/voice`, `POST /ivr/gather`.

A complaint is allowed when a payment has failed, or has been pending or processing for at least
`ESCALATE_AFTER_DAYS` (7, in `procurement/store.py`). That number is a product choice, not a rule.

## 6. Configuration

See `.env.example`. Start as before:

```
export $(cat .env | xargs) && python3 -m uvicorn api:app --host 0.0.0.0 --port 8000
```

Changing the Gemini key means editing `GOOGLE_API_KEY` in `.env` and restarting. The free-tier limit
belongs to the Google project, not the key.

## 7. Tests

```
python3 -m unittest discover tests
```

Covers the store (capacity, queue order, learning estimates, payment rules), the knowledge base,
complaints, notifications (Twilio request format with a fake transport), IVR (signature, TwiML, answers)
and full HTTP journeys through the real routers. The Twilio signature test uses the example value
published in Twilio's own documentation.

## 7a. Demo data for showing the dashboard

For a walkthrough or a recording, real data is often thin (an empty Payments tab until someone has
actually been checked in and completed, for example). `seed_demo_scenario.py` loads a scripted ten-day
scenario — 8 fictional farmers (ids start with `DEMO-`, deliberately not phone-shaped, so they can never
receive a real SMS) — that touches every tab at once: paid, processing, failed and overdue payments, a
live queue with one farmer checked in, a no-show, a cancellation, one complaint with an officer note,
multi-day chat history, and a staff announcement.

```
# stop the backend first
python3 seed_demo_scenario.py          # load the demo scenario
python3 seed_demo_scenario.py --reset  # remove only the demo rows; your real data is untouched
# start the backend again
```

## 8. Optional: SMS and phone calls with Twilio

Nothing here is required. Without it, the app, inbox and dashboard all work.

**SMS.** Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and either `TWILIO_FROM` (a number in +country
format) or `TWILIO_MESSAGING_SERVICE_SID`. Trial accounts can only message numbers you have verified in
Twilio. India requires registered senders and templates for commercial SMS; check Twilio's current India
guidance before promising SMS to real farmers. This code has been tested against a fake transport only,
never against live Twilio.

**Phone calls (IVR).**
1. Run the backend, then expose it: `ngrok http 8000`. Copy the https address.
2. Set `PUBLIC_BASE_URL` to that address (no trailing slash) and set `TWILIO_AUTH_TOKEN`. Restart.
3. In the Twilio console, set your number's "A call comes in" webhook to `PUBLIC_BASE_URL/ivr/voice`,
   method POST.
4. Call from a phone whose number is a registered farmer id. The caller's number is the farmer id, so
   payment and queue answers are personal.

The endpoint refuses to run without a token and a public URL, and rejects any request whose Twilio
signature does not match. To test locally without Twilio, set `IVR_ALLOW_UNSIGNED=1` and:

```
curl -X POST localhost:8000/ivr/voice  -d "From=+919876543210"
curl -X POST localhost:8000/ivr/gather -d "From=+919876543210" -d "SpeechResult=my payment status"
```

Never set `IVR_ALLOW_UNSIGNED` on a server reachable from the internet. Twilio's speech recognition and
voices for Hindi are used as documented but have not been tried live.

## 9. Known limits (say these before a judge asks)

- **No farmer login.** The phone number is the identity, as elsewhere in the app, so anyone who knows a
  number can read that farmer's payments. Real use needs an OTP login first.
- **No live payment lookup.** No public API exposes an individual farmer's payment status. Staff record
  it, and the app explains the reason it is shown.
- **Polling, not push.** The queue refreshes every 15 seconds while the screen is open. Push
  notifications need a full build, not Expo Go.
- **AI and diagnosis need internet.** Offline mode covers reading saved data and queueing actions
  (bookings, cancellations, complaints), not AI answers.
- **SQLite.** Right for a prototype and for per-farmer access. The storage layer is separate, so moving to
  PostgreSQL later is a contained change, but it has not been done or tested.
- **Wait estimates are estimates.** They start from an assumed 6 minutes per farmer and improve with data.
- **Payment failure causes** come from published explainers, not from official NPCI or PFMS documents.
  Verify them, and the two named official channels, before real deployment.
