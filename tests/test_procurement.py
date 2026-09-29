import unittest
from datetime import datetime, timedelta

from farmer_memory import SQLiteStorage
from procurement.store import (
    ESCALATE_AFTER_DAYS, DEFAULT_SERVICE_MINUTES, MIN_SERVICE_MINUTES, InvalidTransition, NotFound,
    ProcurementStore, WindowFull, parse_date,
)

WINDOW = "08:30 AM - 11:30 AM"


class Clock:
    def __init__(self, start=datetime(2026, 9, 16, 8, 0, 0)):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ProcurementStore(SQLiteStorage(":memory:"), now=self.clock)
        self.store.upsert_mandi("m1", "Noida Mandi", 28.5, 77.4)

    def book(self, cid, farmer="9000000001", date="16 Sep 2026", window=WINDOW, mandi="m1"):
        return self.store.book_slot(cid, farmer, "TK-1", mandi, "Wheat", 10.0, date, window)


class TestParseDate(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(parse_date("16 Sep 2026"), "2026-09-16")
        self.assertEqual(parse_date("2026-09-16"), "2026-09-16")
        self.assertEqual(parse_date("1 January 2027"), "2027-01-01")

    def test_rejects_garbage(self):
        for bad in ("", "tomorrow", "32 Sep 2026", "16 Foo 2026"):
            with self.assertRaises(ValueError):
                parse_date(bad)


class TestBooking(StoreCase):
    def test_booking_is_idempotent_on_client_id(self):
        first = self.book("c1")
        again = self.book("c1")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.store.admin_slots()), 1)

    def test_window_capacity_is_enforced_and_cancel_frees_it(self):
        self.store.conn.execute("UPDATE mandis SET capacity_per_window = 2 WHERE id = 'm1'")
        self.book("a"); self.book("b")
        with self.assertRaises(WindowFull):
            self.book("c")
        self.store.cancel_slot("a", "9000000001")
        self.assertTrue(self.book("c")["created"])

    def test_capacity_is_per_window_and_per_date(self):
        self.store.conn.execute("UPDATE mandis SET capacity_per_window = 1 WHERE id = 'm1'")
        self.book("a")
        self.assertTrue(self.book("b", window="11:30 AM - 02:30 PM")["created"])
        self.assertTrue(self.book("c", date="17 Sep 2026")["created"])

    def test_cancel_unknown_or_other_farmers_slot(self):
        self.book("a")
        with self.assertRaises(NotFound):
            self.store.cancel_slot("a", "somebody-else")
        with self.assertRaises(NotFound):
            self.store.cancel_slot("missing", "9000000001")

    def test_bad_date_is_rejected(self):
        with self.assertRaises(ValueError):
            self.book("a", date="not a date")


class TestQueue(StoreCase):
    def positions(self, *ids):
        by_client = {s["client_id"]: s for s in self.store.admin_slots()}
        return [by_client[i]["queue"]["position"] for i in ids]

    def test_booking_order_sets_position(self):
        for cid in ("a", "b", "c"):
            self.book(cid, farmer=f"F{cid}"); self.clock.advance(minutes=1)
        self.assertEqual(self.positions("a", "b", "c"), [1, 2, 3])

    def test_arrived_farmers_are_served_before_absent_ones(self):
        ids = {}
        for cid in ("a", "b", "c"):
            ids[cid] = self.book(cid)["id"]; self.clock.advance(minutes=1)
        self.store.set_slot_status(ids["c"], "checked_in")
        self.assertEqual(self.positions("c", "a", "b"), [1, 2, 3])

    def test_default_estimate_is_flagged_as_not_measured(self):
        for cid in ("a", "b", "c"):
            self.book(cid); self.clock.advance(minutes=1)
        last = [s for s in self.store.admin_slots() if s["client_id"] == "c"][0]["queue"]
        self.assertEqual(last["ahead"], 2)
        self.assertEqual(last["est_wait_minutes"], 2 * DEFAULT_SERVICE_MINUTES)
        self.assertFalse(last["estimate_is_measured"])

    def test_estimate_learns_from_real_service_times(self):
        first = self.book("a")["id"]; second = self.book("b")["id"]; third = self.book("c")["id"]
        self.store.set_slot_status(first, "checked_in")
        self.clock.advance(minutes=10)
        self.store.set_slot_status(first, "completed")
        self.store.set_slot_status(second, "checked_in")
        self.clock.advance(minutes=20)
        self.store.set_slot_status(second, "completed")
        queue = self.store._slot_view(third)["queue"]
        self.assertTrue(queue["estimate_is_measured"])
        self.assertEqual(queue["service_samples"], 2)
        self.assertEqual(queue["ahead"], 0)
        fourth = self.book("d")["id"]
        self.assertEqual(self.store._slot_view(fourth)["queue"]["est_wait_minutes"], 15)

    def test_batch_closed_slots_do_not_poison_the_estimate(self):
        ids = [self.book(f"s{i}", farmer=f"F{i}")["id"] for i in range(4)]
        for slot in ids[:3]:
            self.store.set_slot_status(slot, "checked_in")
            self.clock.advance(seconds=5)
            self.store.set_slot_status(slot, "completed")
        queue = self.store._slot_view(ids[3])["queue"]
        self.assertFalse(queue["estimate_is_measured"])
        self.assertEqual(queue["est_wait_minutes"], 0)
        self.assertEqual(self.store.window_load("16 Sep 2026")[0]["avg_service_minutes"], float(DEFAULT_SERVICE_MINUTES))

    def test_only_plausible_durations_count_when_mixed(self):
        a, b, c = (self.book(x, farmer=f"F{x}")["id"] for x in "abc")
        self.store.set_slot_status(a, "checked_in"); self.clock.advance(seconds=5); self.store.set_slot_status(a, "completed")
        self.store.set_slot_status(b, "checked_in"); self.clock.advance(minutes=12); self.store.set_slot_status(b, "completed")
        queue = self.store._slot_view(c)["queue"]
        self.assertEqual(queue["service_samples"], 1)
        self.assertTrue(queue["estimate_is_measured"])
        self.assertGreaterEqual(12, MIN_SERVICE_MINUTES)

    def test_finished_slots_have_no_queue(self):
        slot = self.book("a")["id"]
        self.store.set_slot_status(slot, "checked_in"); self.store.set_slot_status(slot, "completed")
        self.assertIsNone(self.store._slot_view(slot)["queue"])

    def test_invalid_transitions(self):
        slot = self.book("a")["id"]
        for bad in ("completed", "booked", "nonsense"):
            with self.assertRaises(InvalidTransition):
                self.store.set_slot_status(slot, bad)
        self.store.set_slot_status(slot, "checked_in"); self.store.set_slot_status(slot, "completed")
        with self.assertRaises(InvalidTransition):
            self.store.set_slot_status(slot, "cancelled")
        with self.assertRaises(NotFound):
            self.store.set_slot_status(9999, "checked_in")

    def test_window_load_counts(self):
        a = self.book("a")["id"]; self.book("b"); c = self.book("c")["id"]
        self.store.set_slot_status(a, "checked_in")
        self.store.set_slot_status(c, "cancelled")
        load = self.store.window_load("16 Sep 2026")[0]
        self.assertEqual((load["active"], load["arrived"], load["completed"]), (2, 1, 0))
        self.assertEqual(load["mandi_name"], "Noida Mandi")


class TestPayments(StoreCase):
    def payment(self, **kw):
        args = dict(farmer_id="F1", crop="Wheat", quantity_qtl=10.0, amount=24250.0)
        args.update(kw)
        return self.store.create_payment(**args)

    def test_new_payment_starts_pending_with_one_event(self):
        p = self.payment()
        self.assertEqual(p["status"], "pending")
        self.assertEqual(len(p["timeline"]), 1)

    def test_failure_code_is_kept_only_while_failed(self):
        p = self.payment()
        failed = self.store.update_payment(p["id"], "failed", "name_mismatch", "Aadhaar and bank names differ")
        self.assertEqual(failed["failure_code"], "name_mismatch")
        fixed = self.store.update_payment(p["id"], "processing", "name_mismatch")
        self.assertIsNone(fixed["failure_code"])
        self.assertEqual([e["status"] for e in fixed["timeline"]], ["pending", "failed", "processing"])

    def test_unknown_status_and_payment(self):
        p = self.payment()
        with self.assertRaises(InvalidTransition):
            self.store.update_payment(p["id"], "teleported")
        with self.assertRaises(NotFound):
            self.store.update_payment(999, "paid")

    def test_escalation_rules(self):
        p = self.payment(sold_on="2026-09-16")
        self.assertFalse(p["can_escalate"])
        self.clock.advance(days=ESCALATE_AFTER_DAYS - 1)
        self.assertFalse(self.store.get_payment(p["id"])["can_escalate"])
        self.clock.advance(days=1)
        self.assertTrue(self.store.get_payment(p["id"])["can_escalate"])
        self.assertTrue(self.store.update_payment(p["id"], "failed", "unknown")["can_escalate"])
        paid = self.store.update_payment(p["id"], "paid")
        self.assertFalse(paid["can_escalate"])
        self.assertEqual(paid["days_waiting"], 0)

    def test_linking_a_grievance_stops_further_escalation(self):
        p = self.payment()
        self.store.update_payment(p["id"], "failed")
        self.store.link_grievance(p["id"], 42)
        self.assertFalse(self.store.get_payment(p["id"])["can_escalate"])

    def test_filters_and_farmer_scope(self):
        a = self.payment(); self.payment(farmer_id="F2")
        self.store.update_payment(a["id"], "paid")
        self.assertEqual(len(self.store.farmer_payments("F1")), 1)
        self.assertEqual([p["farmer_id"] for p in self.store.admin_payments("pending")], ["F2"])

    def test_payment_linked_to_slot_carries_mandi_name(self):
        slot = self.book("a")["id"]
        p = self.store.create_payment("F1", "Wheat", 10.0, None, slot_id=slot)
        self.assertEqual(p["mandi_name"], "Noida Mandi")


class TestNotificationsAndStats(StoreCase):
    def test_inbox_read_state_and_isolation(self):
        self.store.add_notification("F1", "k", "T1", "B1"); self.store.add_notification("F2", "k", "T2", "B2")
        self.assertEqual(len(self.store.farmer_notifications("F1")), 1)
        self.assertEqual(self.store.mark_notifications_read("F1"), 1)
        self.assertEqual(self.store.mark_notifications_read("F1"), 0)
        self.assertIsNotNone(self.store.farmer_notifications("F1")[0]["read_at"])
        self.assertIsNone(self.store.farmer_notifications("F2")[0]["read_at"])

    def test_language_preference_round_trip(self):
        self.assertIsNone(self.store.get_language("F1"))
        self.store.set_language("F1", "hi"); self.store.set_language("F1", "mr")
        self.assertEqual(self.store.get_language("F1"), "mr")

    def test_stats(self):
        slot = self.book("a")["id"]; self.book("b")
        self.store.set_slot_status(slot, "checked_in")
        p = self.store.create_payment("F1", "Wheat", 1, 1)
        self.store.create_payment("F1", "Wheat", 1, 1)
        self.store.update_payment(p["id"], "paid")
        stats = self.store.stats()
        self.assertEqual((stats["slots_today"], stats["queue_active"]), (2, 2))
        self.assertEqual((stats["payments_pending"], stats["payments_paid"], stats["payments_failed"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
