import unittest
from datetime import datetime

from farmer_memory import FarmerMemoryModule, SQLiteStorage
from procurement import insights
from procurement.notify import LogSms, NotificationService, phone_e164
from procurement.store import ProcurementStore
from seed_demo_scenario import FARMERS, Clock, reset, seed

NOW = datetime(2026, 9, 28, 14, 0, 0)


class SeedCase(unittest.TestCase):
    def setUp(self):
        self.memory = FarmerMemoryModule(storage=SQLiteStorage(":memory:"), embed_fn=lambda text: [0.0])
        self.clock = Clock(NOW)
        self.store = ProcurementStore(self.memory.storage, now=self.clock)
        self.notifier = NotificationService(self.store, LogSms())
        # a real farmer who must never be affected
        self.store.upsert_farmer("8882355656", "Priya", ["Wheat"], "Gautam Buddh Nagar", "Uttar Pradesh")
        self.store.upsert_mandi("real_mandi", "Real Mandi")
        self.store.book_slot("real-1", "8882355656", "TK-1", "real_mandi", "Wheat", 5, "2026-09-28", "08:30 AM - 11:30 AM")
        self.memory.log_interaction("8882355656", "real question", "real answer", crop="Wheat", embed=False)
        self.result = seed(self.memory, self.store, self.notifier, self.clock, NOW)
        self.store.now = lambda: NOW


class TestSeed(SeedCase):
    def test_every_tab_has_something_to_show(self):
        stats = self.store.stats()
        self.assertGreaterEqual(stats["payments_paid"], 2)
        self.assertGreaterEqual(stats["payments_failed"], 2)
        self.assertGreaterEqual(stats["payments_overdue"], 1)
        self.assertGreaterEqual(stats["amount_paid"], 100000)
        self.assertGreaterEqual(stats["queue_active"], 6)
        self.assertGreaterEqual(len(self.store.admin_slots(status="no_show")), 1)
        self.assertGreaterEqual(len(self.store.admin_slots(status="cancelled")), 1)
        self.assertEqual(len([f for f in self.store.list_registered() if f["farmer_id"].startswith("DEMO-")]), len(FARMERS))
        self.assertTrue(all(f["district"] for f in self.store.list_registered() if f["farmer_id"].startswith("DEMO-")))

    def test_a_live_queue_exists_today_with_one_farmer_already_checked_in(self):
        load = {(r["mandi_name"], r["window"]): r for r in self.store.window_load("2026-09-28")}
        kasna = load[("Greater Noida Kasna Mandi Yard", "08:30 AM - 11:30 AM")]
        self.assertEqual((kasna["active"], kasna["arrived"]), (3, 1))

    def test_a_complaint_is_in_progress_with_a_staff_note_and_linked_to_its_payment(self):
        grievances = self.memory.admin_grievances("in_progress")
        self.assertEqual(len(grievances), 1)
        self.assertIn("Bank contacted", grievances[0]["grievance_note"])
        link = insights.grievance_links(self.store, [grievances[0]["id"]])
        self.assertEqual(list(link.values())[0]["status"], "failed")
        self.assertEqual(len(self.memory.admin_grievances("new")), 1)

    def test_history_spans_many_days_so_charts_are_not_flat(self):
        days = {i["ts"][:10] for i in insights.activity(self.store, 200)}
        self.assertGreaterEqual(len(days), 6)

    def test_demo_farmers_can_never_receive_a_real_sms(self):
        for farmer_id, *_ in FARMERS:
            self.assertIsNone(phone_e164(farmer_id), farmer_id)
        demo = [n for n in self.store.admin_notifications(500) if n["farmer_id"].startswith("DEMO-")]
        self.assertGreater(len(demo), 20)
        self.assertEqual({n["sms_status"] for n in demo}, {"no_phone"})

    def test_running_it_twice_does_not_duplicate_anything(self):
        before = (self.store.stats(), len(self.store.admin_slots()), len(self.store.list_registered()))
        seed(self.memory, self.store, self.notifier, self.clock, NOW)
        self.store.now = lambda: NOW
        self.assertEqual((self.store.stats(), len(self.store.admin_slots()), len(self.store.list_registered())), before)

    def test_it_does_not_touch_real_data(self):
        self.assertEqual(self.store.get_registered("8882355656")["name"], "Priya")
        self.assertEqual(len(self.store.farmer_slots("8882355656")), 1)
        self.assertEqual(len(self.memory.admin_farmer_history("8882355656")), 1)


class TestReset(SeedCase):
    def test_reset_removes_all_demo_data_and_only_demo_data(self):
        removed = reset(self.memory, self.store)
        self.assertEqual(removed["farmers"], len(FARMERS))
        self.assertEqual([f["farmer_id"] for f in self.store.list_registered()], ["8882355656"])
        self.assertEqual([s["farmer_id"] for s in self.store.admin_slots()], ["8882355656"])
        self.assertEqual(self.store.admin_payments(), [])
        self.assertEqual([m["id"] for m in self.store.list_mandis()], ["real_mandi"])
        self.assertEqual(self.store.admin_notifications(500), [])
        self.assertEqual([f["farmer_id"] for f in self.memory.admin_farmers(None)], ["8882355656"])
        self.assertEqual(self.memory.admin_grievances(None), [])
        self.assertEqual(len(self.memory.admin_farmer_history("8882355656")), 1)


if __name__ == "__main__":
    unittest.main()
