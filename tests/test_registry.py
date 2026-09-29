import unittest

from farmer_memory import SQLiteStorage
from procurement.registry import merge_farmers
from procurement.store import ProcurementStore


def chat(farmer_id, last, crops=(), messages=2, grievances=0):
    return {"farmer_id": farmer_id, "message_count": messages, "last_active": last, "first_seen": last,
            "grievance_count": grievances, "crops": list(crops)}


def reg(farmer_id, name, crops=(), when="2026-09-20T10:00:00"):
    return {"farmer_id": farmer_id, "name": name, "primary_crops": list(crops), "registered_at": when, "updated_at": when}


class TestMerge(unittest.TestCase):
    def test_registered_farmer_with_no_chat_appears_with_zero_messages(self):
        rows = merge_farmers([], [reg("8882355656", "Priya", ["Wheat"])])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["name"], rows[0]["message_count"], rows[0]["registered"]), ("Priya", 0, True))
        self.assertEqual(rows[0]["crops"], ["Wheat"])

    def test_chat_only_farmer_has_no_name_and_is_not_marked_registered(self):
        row = merge_farmers([chat("9469455964", "2026-09-17T09:00:00", ["Mustard"])], [])[0]
        self.assertEqual((row["name"], row["registered"], row["message_count"]), (None, False, 2))

    def test_farmer_in_both_keeps_chat_counts_and_gains_a_name(self):
        row = merge_farmers([chat("9469455964", "2026-09-17T09:00:00", ["Mustard"], messages=9)],
                            [reg("9469455964", "Asha", ["Wheat"])])[0]
        self.assertEqual((row["name"], row["message_count"], row["registered"]), ("Asha", 9, True))
        self.assertEqual(row["crops"], ["Mustard"])

    def test_registered_crops_fill_in_only_when_chat_has_none(self):
        row = merge_farmers([chat("F1", "2026-09-17T09:00:00", [])], [reg("F1", "A", ["Paddy"])])[0]
        self.assertEqual(row["crops"], ["Paddy"])

    def test_sorted_by_most_recent_activity(self):
        rows = merge_farmers([chat("old", "2026-09-01T00:00:00"), chat("new", "2026-09-25T00:00:00")],
                             [reg("mid", "M", when="2026-09-10T00:00:00")])
        self.assertEqual([r["farmer_id"] for r in rows], ["new", "mid", "old"])

    def test_search_matches_id_or_name_case_insensitively(self):
        chats = [chat("9469455964", "2026-09-17T09:00:00"), chat("8882321252", "2026-09-16T09:00:00")]
        regs = [reg("8882355656", "Priya Sharma"), reg("9469455964", "Asha")]
        self.assertEqual([r["farmer_id"] for r in merge_farmers(chats, regs, "priya")], ["8882355656"])
        self.assertEqual([r["farmer_id"] for r in merge_farmers(chats, regs, "94694")], ["9469455964"])
        self.assertEqual([r["farmer_id"] for r in merge_farmers(chats, regs, "ASHA")], ["9469455964"])
        self.assertEqual(len(merge_farmers(chats, regs, "  ")), 3)
        self.assertEqual(merge_farmers(chats, regs, "zzz"), [])

    def test_a_name_search_keeps_the_real_chat_counts(self):
        row = merge_farmers([chat("F1", "2026-09-17T09:00:00", messages=9)], [reg("F1", "Asha")], "asha")[0]
        self.assertEqual(row["message_count"], 9)

    def test_inputs_are_not_mutated(self):
        chats = [chat("F1", "2026-09-17T09:00:00")]
        regs = [reg("F1", "Asha", ["Wheat"])]
        merge_farmers(chats, regs)
        self.assertNotIn("name", chats[0])
        merge_farmers([], regs)[0]["crops"].append("x")
        self.assertEqual(regs[0]["primary_crops"], ["Wheat"])

    def test_empty(self):
        self.assertEqual(merge_farmers([], []), [])


class TestRegistryStore(unittest.TestCase):
    def setUp(self):
        self.store = ProcurementStore(SQLiteStorage(":memory:"))

    def test_round_trip_including_hindi_crop_names(self):
        saved = self.store.upsert_farmer("8882355656", "प्रिया", ["गेहूं", "Paddy"])
        self.assertEqual(saved["primary_crops"], ["गेहूं", "Paddy"])
        self.assertEqual(self.store.get_registered("8882355656")["name"], "प्रिया")

    def test_upsert_updates_in_place_and_keeps_the_original_registration_time(self):
        self.store.upsert_farmer("F1", "Old", ["Wheat"])
        first = self.store.get_registered("F1")
        self.store.now = lambda: __import__("datetime").datetime(2030, 1, 1)
        self.store.upsert_farmer("F1", "New", ["Rice"])
        second = self.store.get_registered("F1")
        self.assertEqual((second["name"], second["primary_crops"]), ("New", ["Rice"]))
        self.assertEqual(len(self.store.list_registered()), 1)
        self.assertEqual(second["registered_at"], first["registered_at"])
        self.assertGreater(second["updated_at"], first["updated_at"])

    def test_unknown_farmer(self):
        self.assertIsNone(self.store.get_registered("nobody"))


if __name__ == "__main__":
    unittest.main()
