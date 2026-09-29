import sqlite3
import unittest
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from farmer_memory import FarmerMemoryModule, SQLiteStorage
from procurement import insights
from procurement.notify import LogSms, NotificationService, render
from procurement.registry import merge_farmers
from procurement.store import NotFound, ProcurementStore, WindowFull
from routes import farmer_routes, staff_routes
from routes.deps import Deps

TOKEN = "staff-secret"
STAFF = {"X-Admin-Token": TOKEN}
WINDOW = "08:30 AM - 11:30 AM"


class Clock:
    def __init__(self):
        self.t = datetime(2026, 9, 20, 9, 0, 0)

    def __call__(self):
        return self.t


def make_client():
    memory = FarmerMemoryModule(storage=SQLiteStorage(":memory:"), embed_fn=lambda text: [0.0])
    clock = Clock()
    store = ProcurementStore(memory.storage, now=clock)

    def check_admin(value):
        if value != TOKEN:
            raise HTTPException(401, "Invalid or missing admin token")

    deps = Deps(memory=memory, store=store, notifications=NotificationService(store, LogSms()), check_admin=check_admin)
    app = FastAPI()
    for module in (farmer_routes, staff_routes):
        app.include_router(module.build(deps))
    return TestClient(app), deps, clock


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = ProcurementStore(SQLiteStorage(":memory:"), now=self.clock)
        self.store.upsert_mandi("m1", "Noida Mandi", 28.5, 77.4)

    def book(self, cid, farmer="F1", window=WINDOW, date="20 Sep 2026", crop="Wheat"):
        return self.store.book_slot(cid, farmer, "TK", "m1", crop, 10.0, date, window)


class TestMigrationAndLocation(unittest.TestCase):
    def test_existing_registry_table_gets_location_columns_without_losing_rows(self):
        db = SQLiteStorage(":memory:")
        db.conn.executescript("""
            CREATE TABLE farmer_registry (farmer_id TEXT PRIMARY KEY, name TEXT, primary_crops TEXT,
                registered_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO farmer_registry VALUES ('8882355656', 'Priya', '["Wheat"]', '2026-09-28T10:00:00', '2026-09-28T10:00:00');
        """)
        store = ProcurementStore(db)
        row = store.get_registered("8882355656")
        self.assertEqual((row["name"], row["district"], row["state"]), ("Priya", None, None))
        store.upsert_farmer("8882355656", "Priya", ["Wheat"], "Gautam Buddh Nagar", "Uttar Pradesh")
        self.assertEqual(store.get_registered("8882355656")["district"], "Gautam Buddh Nagar")
        ProcurementStore(db)

    def test_a_later_sync_without_location_does_not_erase_a_known_location(self):
        store = ProcurementStore(SQLiteStorage(":memory:"))
        store.upsert_farmer("F1", "A", [], "Nagpur", "Maharashtra")
        store.upsert_farmer("F1", "A", ["Wheat"], None, None)
        self.assertEqual((store.get_registered("F1")["district"], store.get_registered("F1")["state"]), ("Nagpur", "Maharashtra"))


class TestMandiCapacity(StoreCase):
    def test_capacity_can_be_changed_and_is_enforced(self):
        self.assertEqual(self.store.set_mandi_capacity("m1", 1)["capacity_per_window"], 1)
        self.book("a")
        with self.assertRaises(WindowFull):
            self.book("b", farmer="F2")

    def test_boundaries_and_bad_values(self):
        self.assertEqual(self.store.set_mandi_capacity("m1", 500)["capacity_per_window"], 500)
        for bad in (0, -1, 501, True, "10", 10.5, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.store.set_mandi_capacity("m1", bad)

    def test_unknown_mandi(self):
        with self.assertRaises(NotFound):
            self.store.set_mandi_capacity("nope", 5)

    def test_listing(self):
        self.assertEqual([m["name"] for m in self.store.list_mandis()], ["Noida Mandi"])


class TestErasure(StoreCase):
    def populate(self, farmer):
        slot = self.book(f"c-{farmer}", farmer=farmer)["id"]
        pay = self.store.create_payment(farmer, "Wheat", 5, 1000, slot_id=slot)
        self.store.update_payment(pay["id"], "failed", "name_mismatch")
        self.store.add_notification(farmer, "k", "T", "B")
        self.store.set_language(farmer, "hi")
        self.store.upsert_farmer(farmer, "Name", ["Wheat"])

    def test_everything_about_one_farmer_is_removed_and_nobody_else_is_touched(self):
        self.populate("F1"); self.populate("F2")
        counts = self.store.erase_farmer("F1")
        self.assertEqual(counts, {"payment_events": 2, "payments": 1, "slots": 1, "notifications": 1, "preferences": 1, "registry": 1})
        self.assertEqual(self.store.farmer_slots("F1"), [])
        self.assertEqual(self.store.farmer_payments("F1"), [])
        self.assertEqual(self.store.farmer_notifications("F1"), [])
        self.assertIsNone(self.store.get_language("F1"))
        self.assertIsNone(self.store.get_registered("F1"))
        self.assertEqual(len(self.store.farmer_slots("F2")), 1)
        self.assertEqual(len(self.store.farmer_payments("F2")[0]["timeline"]), 2)
        self.assertEqual(self.store.get_registered("F2")["name"], "Name")

    def test_erasing_an_unknown_farmer_is_harmless(self):
        self.assertEqual(sum(self.store.erase_farmer("nobody").values()), 0)

    def test_no_orphaned_payment_events_remain(self):
        self.populate("F1")
        self.store.erase_farmer("F1")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()["c"], 0)


class TestStats(StoreCase):
    def test_amounts_overdue_and_daily_counts(self):
        paid = self.store.create_payment("F1", "Wheat", 1, 5000, sold_on="2026-09-19")
        self.store.update_payment(paid["id"], "paid")
        self.store.create_payment("F2", "Wheat", 1, 3000, sold_on="2026-09-10")
        self.store.create_payment("F3", "Wheat", 1, 2000, sold_on="2026-09-19")
        failed = self.store.create_payment("F4", "Wheat", 1, 700, sold_on="2026-09-19")
        self.store.update_payment(failed["id"], "failed")
        gone = self.book("a")["id"]; self.store.set_slot_status(gone, "no_show")
        done = self.book("b", farmer="F9")["id"]; self.store.set_slot_status(done, "checked_in"); self.store.set_slot_status(done, "completed")
        stats = self.store.stats()
        self.assertEqual((stats["amount_paid"], stats["amount_pending"], stats["amount_failed"]), (5000.0, 5000.0, 700.0))
        self.assertEqual(stats["payments_overdue"], 1)
        self.assertEqual((stats["no_shows_today"], stats["completed_today"]), (1, 1))


class TestInsights(StoreCase):
    def test_activity_merges_every_source_newest_first_with_names(self):
        self.store.upsert_farmer("F1", "Priya", ["Wheat"])
        self.clock.t += timedelta(minutes=1)
        slot = self.book("a")["id"]
        self.clock.t += timedelta(minutes=1)
        self.store.set_slot_status(slot, "checked_in")
        self.clock.t += timedelta(minutes=1)
        self.store.set_slot_status(slot, "completed")
        pay = self.store.create_payment("F1", "Wheat", 10, 24250, slot_id=slot)
        self.clock.t += timedelta(minutes=1)
        self.store.update_payment(pay["id"], "failed", "name_mismatch")
        self.store.conn.execute("INSERT INTO interactions (farmer_id, ts, query_text, response_text, is_grievance) VALUES (?, ?, ?, ?, ?)",
                                ("F1", (self.clock.t + timedelta(minutes=1)).isoformat(), "wheat rust?\nplease help", "ans", 0))
        items = insights.activity(self.store, 50)
        kinds = [i["kind"] for i in items]
        self.assertEqual(kinds[0], "chat")
        for expected in ("registered", "slot_booked", "slot_checked_in", "slot_completed", "payment"):
            self.assertIn(expected, kinds)
        self.assertEqual(items[0]["farmer_name"], "Priya")
        self.assertEqual(items[0]["data"]["text"], "wheat rust? please help")
        self.assertEqual([i["ts"] for i in items], sorted((i["ts"] for i in items), reverse=True))
        failed = [i for i in items if i["kind"] == "payment" and i["data"]["status"] == "failed"][0]
        self.assertEqual((failed["data"]["reason"], failed["data"]["amount"]), ("name_mismatch", 24250))

    def test_activity_limit_and_empty(self):
        self.assertEqual(insights.activity(self.store), [])
        for i in range(5):
            self.store.upsert_farmer(f"F{i}", "N", [])
        self.assertEqual(len(insights.activity(self.store, 3)), 3)
        self.assertEqual(len(insights.activity(self.store, -5)), 1)

    def test_trends(self):
        a = self.book("a")["id"]
        self.store.set_slot_status(a, "checked_in"); self.store.set_slot_status(a, "completed")
        self.book("b", farmer="F2")
        self.clock.t += timedelta(days=1)
        self.book("c", farmer="F3")
        self.store.create_payment("F1", "Wheat", 1, 1000)
        data = insights.trends(self.store, 3)
        self.assertEqual([d["date"] for d in data["days"]], ["2026-09-19", "2026-09-20", "2026-09-21"])
        self.assertEqual([(d["booked"], d["completed"]) for d in data["days"]], [(0, 0), (2, 1), (1, 0)])
        self.assertEqual(data["payments"]["pending"], {"count": 1, "amount": 1000.0})
        self.assertEqual(data["payments"]["paid"], {"count": 0, "amount": 0.0})

    def test_farmer_counts_ignore_cancelled_slots(self):
        a = self.book("a")["id"]; self.book("b")
        self.store.set_slot_status(a, "cancelled")
        pay = self.store.create_payment("F1", "Wheat", 1, 10); self.store.create_payment("F1", "Wheat", 1, 10)
        self.store.update_payment(pay["id"], "paid")
        self.assertEqual(insights.farmer_counts(self.store)["F1"], {"slots": 1, "payments": 2, "open_payments": 1})

    def test_grievance_links(self):
        pay = self.store.create_payment("F1", "Wheat", 1, 999)
        self.store.link_grievance(pay["id"], 7)
        self.assertEqual(insights.grievance_links(self.store, [7, 8])[7]["amount"], 999)
        self.assertNotIn(8, insights.grievance_links(self.store, [7, 8]))
        self.assertEqual(insights.grievance_links(self.store, []), {})


class TestMergeExtras(unittest.TestCase):
    def test_language_location_counts_and_search_by_district(self):
        reg = [{"farmer_id": "F1", "name": "Priya", "primary_crops": ["Wheat"], "registered_at": "2026-09-01T00:00:00",
                "updated_at": "2026-09-01T00:00:00", "district": "Nagpur", "state": "Maharashtra"}]
        rows = merge_farmers([], reg, None, {"F1": "hi"}, {"F1": {"slots": 2, "payments": 1, "open_payments": 1}})
        self.assertEqual((rows[0]["language"], rows[0]["location"], rows[0]["slot_count"], rows[0]["open_payments"]), ("hi", "Nagpur, Maharashtra", 2, 1))
        self.assertEqual(len(merge_farmers([], reg, "nagpur")), 1)
        self.assertEqual(merge_farmers([], reg, "pune"), [])

    def test_a_farmer_without_registration_has_no_location(self):
        row = merge_farmers([{"farmer_id": "F1", "message_count": 1, "last_active": "x", "first_seen": "x", "grievance_count": 0, "crops": []}], [])[0]
        self.assertEqual((row["location"], row["slot_count"], row["language"]), (None, 0, None))


class TestAnnouncementRender(unittest.TestCase):
    def test_free_text_is_used_verbatim_and_tidied(self):
        self.assertEqual(render("announcement", "hi", {"title": " Mandi closed ", "body": "Closed\n on   Sunday."}), ("Mandi closed", "Closed on Sunday."))


class TestDashboardRoutes(unittest.TestCase):
    def setUp(self):
        self.client, self.deps, self.clock = make_client()
        self.client.put("/farmer/9111111111/profile", json={"name": "Ravi", "primary_crops": ["Wheat"], "district": "Nagpur", "state": "Maharashtra"})
        self.client.put("/farmer/9222222222/profile", json={"name": "Meena", "primary_crops": ["Rice"]})

    def test_every_new_admin_endpoint_needs_the_token(self):
        for method, path, body in (("get", "/admin/activity", None), ("get", "/admin/procurement-trends", None), ("get", "/admin/mandis", None),
                                   ("patch", "/admin/mandis/m1", {"capacity_per_window": 5}), ("post", "/admin/announce", {"title": "a", "body": "b"}),
                                   ("get", "/admin/grievances", None), ("patch", "/admin/grievances/1", {"status": "resolved"})):
            r = getattr(self.client, method)(path, **({"json": body} if body else {}))
            self.assertEqual(r.status_code, 401, path)

    def test_announce_to_everyone_reaches_every_inbox(self):
        r = self.client.post("/admin/announce", json={"title": "Mandi closed Sunday", "body": "The centre is closed on Sunday."}, headers=STAFF)
        self.assertEqual(r.json(), {"sent": 2})
        for farmer in ("9111111111", "9222222222"):
            inbox = self.client.get(f"/farmer/{farmer}/notifications").json()
            self.assertEqual((inbox[0]["title"], inbox[0]["body"]), ("Mandi closed Sunday", "The centre is closed on Sunday."))

    def test_announce_to_one_farmer_only(self):
        self.client.post("/admin/announce", json={"title": "Hello", "body": "Just you.", "farmer_id": "9111111111"}, headers=STAFF)
        self.assertEqual(len(self.client.get("/farmer/9111111111/notifications").json()), 1)
        self.assertEqual(self.client.get("/farmer/9222222222/notifications").json(), [])

    def test_announce_validation(self):
        bad = ({"title": "  ", "body": "x"}, {"title": "x" * 81, "body": "x"}, {"title": "x", "body": ""}, {"title": "x", "body": "y" * 501})
        for body in bad:
            self.assertEqual(self.client.post("/admin/announce", json=body, headers=STAFF).status_code, 422, body)
        self.assertEqual(self.client.post("/admin/announce", json={"title": "a", "body": "b", "farmer_id": "0000000000"}, headers=STAFF).status_code, 404)

    def test_changing_a_mandi_capacity_takes_effect_on_bookings(self):
        book = lambda cid, farmer: self.client.post("/slots", json={"client_id": cid, "farmer_id": farmer, "token": "T", "mandi_id": "m1",
            "mandi_name": "Noida", "crop": "Wheat", "quantity_qtl": 1, "date": "20 Sep 2026", "window": WINDOW})
        book("c0", "9111111111")
        r = self.client.patch("/admin/mandis/m1", json={"capacity_per_window": 1}, headers=STAFF)
        self.assertEqual(r.json()["capacity_per_window"], 1)
        self.assertEqual(book("c1", "9222222222").status_code, 409)
        self.assertEqual(self.client.patch("/admin/mandis/m1", json={"capacity_per_window": 0}, headers=STAFF).status_code, 422)
        self.assertEqual(self.client.patch("/admin/mandis/nope", json={"capacity_per_window": 5}, headers=STAFF).status_code, 404)
        self.assertEqual(self.client.get("/admin/mandis", headers=STAFF).json()[0]["capacity_per_window"], 1)

    def test_farmer_list_carries_language_location_and_counts(self):
        self.client.put("/farmer/9111111111/prefs", json={"language": "hi"})
        self.client.post("/slots", json={"client_id": "c1", "farmer_id": "9111111111", "token": "T", "mandi_id": "m1", "mandi_name": "N",
                                        "crop": "Wheat", "quantity_qtl": 1, "date": "20 Sep 2026", "window": WINDOW})
        row = {r["farmer_id"]: r for r in self.client.get("/admin/farmers", headers=STAFF).json()}["9111111111"]
        self.assertEqual((row["language"], row["location"], row["slot_count"]), ("hi", "Nagpur, Maharashtra", 1))

    def test_farmer_detail_is_a_complete_picture(self):
        self.client.post("/slots", json={"client_id": "c1", "farmer_id": "9111111111", "token": "TK-1", "mandi_id": "m1", "mandi_name": "N",
                                        "crop": "Wheat", "quantity_qtl": 5, "date": "20 Sep 2026", "window": WINDOW})
        sid = self.client.get("/admin/slots", headers=STAFF).json()[0]["id"]
        self.client.patch(f"/admin/slots/{sid}", json={"status": "checked_in"}, headers=STAFF)
        pid = self.client.patch(f"/admin/slots/{sid}", json={"status": "completed", "amount": 12000}, headers=STAFF).json()["payment_id"]
        self.client.patch(f"/admin/payments/{pid}", json={"status": "failed", "failure_code": "name_mismatch"}, headers=STAFF)
        self.client.post(f"/farmer/9111111111/payments/{pid}/escalate", json={})
        d = self.client.get("/admin/farmers/9111111111", headers=STAFF).json()
        self.assertEqual((d["name"], d["location"], len(d["slots"]), len(d["payments"]), len(d["complaints"])), ("Ravi", "Nagpur, Maharashtra", 1, 1, 1))
        self.assertEqual(d["payments"][0]["farmer_name"], "Ravi")

    def test_complaint_list_shows_the_farmer_and_the_payment_it_is_about(self):
        self.client.post("/admin/payments", json={"farmer_id": "9111111111", "crop": "Wheat", "amount": 8000, "sold_on": "2026-09-01"}, headers=STAFF)
        pid = self.client.get("/admin/payments", headers=STAFF).json()[0]["id"]
        self.client.patch(f"/admin/payments/{pid}", json={"status": "failed", "failure_code": "aadhaar_not_seeded"}, headers=STAFF)
        gid = self.client.post(f"/farmer/9111111111/payments/{pid}/escalate", json={}).json()["grievance_id"]
        row = self.client.get("/admin/grievances", headers=STAFF).json()[0]
        self.assertEqual((row["id"], row["farmer_name"], row["payment"]["amount"], row["payment"]["payment_id"]), (gid, "Ravi", 8000, pid))
        r = self.client.patch(f"/admin/grievances/{gid}", json={"status": "in_progress", "note": "Bank contacted"}, headers=STAFF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.patch(f"/admin/grievances/{gid}", json={"status": "bogus"}, headers=STAFF).status_code, 400)
        self.assertEqual(self.client.patch("/admin/grievances/9999", json={"status": "resolved"}, headers=STAFF).status_code, 404)
        self.assertEqual(self.client.get("/admin/grievances?status=in_progress", headers=STAFF).json()[0]["id"], gid)
        self.assertEqual(self.client.get("/admin/grievances?status=resolved", headers=STAFF).json(), [])
        titles = [n["title"] for n in self.client.get("/farmer/9111111111/notifications").json()]
        self.assertIn("Complaint update", titles)

    def test_activity_and_trends_endpoints(self):
        items = self.client.get("/admin/activity?limit=5", headers=STAFF).json()
        self.assertEqual({i["kind"] for i in items}, {"registered"})
        self.assertEqual(len(self.client.get("/admin/procurement-trends?days=7", headers=STAFF).json()["days"]), 7)

    def test_deleting_a_farmer_removes_them_from_the_dashboard_everywhere(self):
        self.client.post("/slots", json={"client_id": "c1", "farmer_id": "9111111111", "token": "T", "mandi_id": "m1", "mandi_name": "N",
                                        "crop": "Wheat", "quantity_qtl": 5, "date": "20 Sep 2026", "window": WINDOW})
        self.deps.memory.log_interaction("9111111111", "wheat rust?", "answer", crop="Wheat")
        r = self.client.delete("/farmer/9111111111")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["rows_deleted"], r.json()["erased"]["slots"], r.json()["erased"]["registry"]), (1, 1, 1))
        self.assertEqual([f["farmer_id"] for f in self.client.get("/admin/farmers", headers=STAFF).json()], ["9222222222"])
        self.assertEqual(self.client.get("/admin/farmers/9111111111", headers=STAFF).status_code, 404)
        self.assertEqual(self.client.get("/admin/slots", headers=STAFF).json(), [])
        self.assertEqual(self.client.get("/farmer/9111111111/notifications").json(), [])
        self.assertNotIn("9111111111", [i["farmer_id"] for i in self.client.get("/admin/activity", headers=STAFF).json()])


if __name__ == "__main__":
    unittest.main()
