#!/usr/bin/env python3
"""Loads a scripted demo scenario so every dashboard tab has realistic content.

    python3 seed_demo_scenario.py            load the scenario (safe to run again)
    python3 seed_demo_scenario.py --reset    remove it again

Stop the backend first, run this, then start the backend. Your real farmers and their data are never touched.

The scenario is FICTIONAL: eight demo farmers whose IDs start with DEMO- (deliberately not phone numbers, so no SMS
can ever reach a real person). Tell anyone you show it to that it is scripted demo data alongside the live app.
"""
import argparse
import os
from datetime import datetime, timedelta

from farmer_memory import FarmerMemoryModule, SQLiteStorage
from procurement.complaint import build_complaint
from procurement.notify import LogSms, NotificationService
from procurement.store import ProcurementStore

DEMO_PREFIX = "DEMO-"

FARMERS = [
    ("DEMO-01", "Ramesh Yadav", ["Wheat"], "Gautam Buddh Nagar", "Uttar Pradesh", "en"),
    ("DEMO-02", "Sunita Devi", ["Paddy"], "Karnal", "Haryana", "hi"),
    ("DEMO-03", "Gurpreet Singh", ["Wheat", "Mustard"], "Ludhiana", "Punjab", "en"),
    ("DEMO-04", "Lakshmi Bai", ["Cotton"], "Nagpur", "Maharashtra", "en"),
    ("DEMO-05", "Mohan Lal", ["Mustard"], "Alwar", "Rajasthan", "hi"),
    ("DEMO-06", "Anita Kumari", ["Paddy"], "Patna", "Bihar", "hi"),
    ("DEMO-07", "Suresh Patel", ["Maize"], "Vadodara", "Gujarat", "en"),
    ("DEMO-08", "Kavita Sharma", ["Wheat"], "Meerut", "Uttar Pradesh", "en"),
]

MANDIS = {
    "demo_kasna": ("Greater Noida Kasna Mandi Yard", 28.45, 77.52),
    "demo_karnal": ("Karnal Anaj Mandi", 29.69, 76.99),
    "demo_ludhiana": ("Ludhiana Grain Market", 30.90, 75.85),
    "demo_alwar": ("Alwar Krishi Upaj Mandi", 27.56, 76.61),
}

W1, W2, W3 = "08:30 AM - 11:30 AM", "11:30 AM - 02:30 PM", "02:30 PM - 05:30 PM"


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


def _iso(day: datetime) -> str:
    return day.date().isoformat()


def reset(memory, store) -> dict:
    removed = {"farmers": 0, "slots": 0, "payments": 0}
    for farmer_id, *_ in FARMERS:
        memory.forget_farmer(farmer_id)
        erased = store.erase_farmer(farmer_id)
        removed["farmers"] += erased["registry"]
        removed["slots"] += erased["slots"]
        removed["payments"] += erased["payments"]
    with store.lock:
        store.conn.execute("DELETE FROM mandis WHERE id LIKE 'demo\\_%' ESCAPE '\\'")
        store.conn.commit()
    return removed


def seed(memory, store, notifier, clock: Clock, now: datetime) -> dict:
    reset(memory, store)
    at = lambda days_ago, hour, minute=0: (now - timedelta(days=days_ago)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    def set_time(moment: datetime):
        clock.t = moment

    for i, (farmer_id, name, crops, district, state, language) in enumerate(FARMERS):
        set_time(at(11 - i % 3, 9 + i))
        store.upsert_farmer(farmer_id, name, crops, district, state)
        store.set_language(farmer_id, language)
    for mandi_id, (name, lat, lon) in MANDIS.items():
        store.upsert_mandi(mandi_id, name, lat, lon)

    created = {"slots": 0, "payments": 0}

    def sale(days_ago, farmer, mandi, crop, qty, amount, window, path):
        """A completed slot with its payment. `path` lists (days after sale, status, reason, note) payment steps."""
        day = at(days_ago, 9, 10)
        set_time(day - timedelta(days=1))
        slot = store.book_slot(f"demo-{farmer}-{days_ago}", farmer, f"TK-{50 + created['slots']}", mandi, crop, qty, _iso(day), window)
        notifier.notify(farmer, "slot_booked", mandi=MANDIS[mandi][0], date=_iso(day), window=window, token=slot["token"])
        set_time(day)
        store.set_slot_status(slot["id"], "checked_in")
        set_time(day + timedelta(minutes=14))
        store.set_slot_status(slot["id"], "completed")
        notifier.notify(farmer, "slot_completed", mandi=MANDIS[mandi][0])
        payment = store.create_payment(farmer, crop, qty, amount, sold_on=_iso(day), slot_id=slot["id"])
        created["slots"] += 1
        created["payments"] += 1
        for after_days, status, reason, note in path:
            set_time(day + timedelta(days=after_days, hours=1))
            payment = store.update_payment(payment["id"], status, reason, note)
            notifier.notify(farmer, "payment_update", crop=crop, status=status.capitalize(), detail=note or "")
        return payment

    def booking(days_ago, farmer, mandi, crop, qty, window, status=None):
        day = at(days_ago, 8, 0)
        set_time(day if days_ago else now - timedelta(hours=3))
        slot = store.book_slot(f"demo-{farmer}-b{days_ago}-{window[:2]}", farmer, f"TK-{50 + created['slots']}", mandi, crop, qty, _iso(at(days_ago, 8)), window)
        notifier.notify(farmer, "slot_booked", mandi=MANDIS[mandi][0], date=_iso(at(days_ago, 8)), window=window, token=slot["token"])
        created["slots"] += 1
        if status == "checked_in":
            set_time(now - timedelta(minutes=25))
            store.set_slot_status(slot["id"], "checked_in")
        elif status:
            set_time(day + timedelta(hours=3))
            store.set_slot_status(slot["id"], status)
        return slot

    # ---- the last ten days ----
    sale(10, "DEMO-08", "demo_kasna", "Wheat", 12, 29100, W1, [(0, "processing", None, "Sent to the bank")])       # stuck, overdue
    sale(6, "DEMO-01", "demo_kasna", "Wheat", 20, 48500, W1, [(1, "processing", None, None), (3, "paid", None, "Credited")])
    failed = sale(5, "DEMO-02", "demo_karnal", "Paddy", 30, 65400, W2, [(1, "failed", "name_mismatch", "Bank rejected the transfer")])
    sale(5, "DEMO-03", "demo_ludhiana", "Wheat", 40, 97000, W1, [(2, "paid", None, "Credited")])
    sale(4, "DEMO-05", "demo_alwar", "Mustard", 15, 82500, W2, [(1, "processing", None, None)])
    sale(3, "DEMO-07", "demo_kasna", "Maize", 25, 57500, W3, [(1, "failed", "aadhaar_not_seeded", "Aadhaar not linked for payments")])
    booking(3, "DEMO-06", "demo_karnal", "Paddy", 18, W1, status="no_show")
    booking(2, "DEMO-04", "demo_ludhiana", "Cotton", 10, W2, status="cancelled")

    # ---- today: a live queue ----
    booking(0, "DEMO-01", "demo_kasna", "Wheat", 25, W1, status="checked_in")
    booking(0, "DEMO-08", "demo_kasna", "Wheat", 12, W1)
    booking(0, "DEMO-03", "demo_kasna", "Mustard", 20, W1)
    booking(0, "DEMO-02", "demo_karnal", "Paddy", 30, W2)
    booking(0, "DEMO-05", "demo_ludhiana", "Mustard", 15, W3)

    # ---- one payment failure becomes a complaint, and staff answer it ----
    set_time(at(2, 10))
    payment = store.get_payment(failed["id"])
    complaint = build_complaint(payment, "DEMO-02", "en")
    grievance = memory.log_interaction("DEMO-02", complaint, "Complaint registered from Where's my money. Awaiting officer review.",
                                       crop="Paddy", intent="payment_escalation", is_grievance=True, timestamp=clock.t, embed=False)
    store.link_grievance(failed["id"], grievance)
    notifier.notify("DEMO-02", "complaint_registered", ref=f"#{grievance}")
    set_time(at(1, 15))
    memory.admin_update_grievance(grievance, "in_progress", "Bank contacted. The account name will be corrected and the payment retried.")
    notifier.notify("DEMO-02", "complaint_update", ref=f"#{grievance}", status="In progress", note="Bank contacted. The account name will be corrected and the payment retried.")

    # ---- a complaint raised in chat, and everyday questions ----
    chats = [
        ("DEMO-06", 3, 11, "I sold my paddy 3 weeks ago and still have not been paid", "I'm sorry about the delay. I have noted this as a payment complaint so an officer can check it.", "Paddy", True),
        ("DEMO-01", 8, 10, "What is the MSP for wheat this season?", "The MSP for wheat is announced by the Government of India each season. Please confirm the current rate with your procurement centre.", "Wheat", False),
        ("DEMO-01", 6, 8, "How do I remove husk and stones before taking my wheat to the centre?", "Winnow or sieve the grain to remove chaff and stones, and dry it in the sun so it is clean when you arrive.", "Wheat", False),
        ("DEMO-03", 5, 9, "There are aphids on my mustard crop, what should I do?", "Check the underside of leaves and consult your local agriculture officer for a suitable treatment for your area.", "Mustard", False),
        ("DEMO-05", 4, 10, "मेरी सरसों का भुगतान कब तक आएगा?", "भुगतान की स्थिति आप ऐप में 'मेरी स्थिति' में देख सकते हैं।", "Mustard", False),
        ("DEMO-07", 3, 9, "How should I store maize after harvest?", "Dry it well before storage and keep it in a cool, ventilated place away from moisture.", "Maize", False),
        ("DEMO-02", 1, 16, "How long does paddy payment take after selling?", "It varies. You can track your payment under My status in the app.", "Paddy", False),
    ]
    for farmer, days_ago, hour, question, answer, crop, is_grievance in chats:
        memory.log_interaction(farmer, question, answer, crop=crop, timestamp=at(days_ago, hour), is_grievance=is_grievance, embed=False)

    # ---- a message from staff ----
    set_time(at(1, 12))
    for farmer_id, *_ in FARMERS:
        notifier.notify(farmer_id, "announcement", title="Mandi timings this week",
                        body="Procurement centres are open from 8:30 AM to 5:30 PM. Please bring your gate pass and bank passbook.")
    return {**created, "farmers": len(FARMERS)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="kissan_saathi.db")
    parser.add_argument("--reset", action="store_true", help="remove the demo scenario")
    args = parser.parse_args()
    if not os.path.exists(args.db):
        raise SystemExit(f"Database {args.db} not found. Run this from the backend folder, with the backend stopped.")
    memory = FarmerMemoryModule(storage=SQLiteStorage(args.db), embed_fn=lambda text: [0.0])
    clock = Clock(datetime.utcnow())
    store = ProcurementStore(memory.storage, now=clock)
    if args.reset:
        print("Removed demo data:", reset(memory, store))
        return
    result = seed(memory, store, NotificationService(store, LogSms()), clock, datetime.utcnow())
    print(f"Loaded demo scenario: {result['farmers']} demo farmers, {result['slots']} slots, {result['payments']} payments.")
    print("These records are FICTIONAL (IDs start with DEMO-). Say so when presenting. Remove them with --reset.")


if __name__ == "__main__":
    main()
