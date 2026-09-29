import unittest
from datetime import datetime, timedelta
from farmer_memory import FarmerMemoryModule, SQLiteStorage


class TestFarmerMemoryModule(unittest.TestCase):
    def setUp(self):
        self.memory = FarmerMemoryModule(storage=SQLiteStorage(":memory:"))

    def test_log_and_retrieve(self):
        self.memory.log_interaction("F1", "wheat pest aphid yellow leaves", "spray imidacloprid")
        ctx = self.memory.retrieve_context("F1", "wheat pest aphid yellow leaves again")
        self.assertTrue(len(ctx.relevant_interactions) >= 1)

    def test_crop_filtering(self):
        self.memory.log_interaction("F1", "wheat msp price query", "2425 per quintal", crop="Wheat")
        self.memory.log_interaction("F1", "paddy msp price query", "2300 per quintal", crop="Paddy")
        ctx = self.memory.retrieve_context("F1", "wheat msp price query", crop="Wheat")
        for it in ctx.relevant_interactions:
            self.assertEqual(it.crop, "Wheat")

    def test_farmer_isolation(self):
        self.memory.log_interaction("F1", "wheat pest aphid yellow leaves", "spray imidacloprid")
        ctx = self.memory.retrieve_context("F2", "wheat pest aphid yellow leaves")
        self.assertEqual(len(ctx.relevant_interactions), 0)

    def test_recency_vs_relevance(self):
        now = datetime.utcnow()
        self.memory.log_interaction("F1", "wheat pest aphid yellow leaves", "old answer", timestamp=now - timedelta(days=170))
        self.memory.log_interaction("F1", "unrelated payment query", "payment answer", timestamp=now)
        ctx = self.memory.retrieve_context("F1", "wheat pest aphid yellow leaves problem")
        self.assertTrue(any("aphid" in it.query_text for it in ctx.relevant_interactions))

    def test_lookback_window(self):
        now = datetime.utcnow()
        self.memory.log_interaction("F1", "wheat pest aphid yellow leaves", "old", timestamp=now - timedelta(days=800))
        ctx = self.memory.retrieve_context("F1", "wheat pest aphid yellow leaves", lookback_months=24)
        self.assertEqual(len(ctx.relevant_interactions), 0)

    def test_profile_consolidation(self):
        self.memory.log_interaction("F1", "wheat query", "answer", crop="Wheat")
        profile = self.memory.consolidate_profile("F1")
        self.assertIn("Wheat", profile.structured_facts["crops"])

    def test_forget_farmer(self):
        self.memory.log_interaction("F1", "wheat query", "answer")
        deleted = self.memory.forget_farmer("F1")
        self.assertGreater(deleted, 0)
        ctx = self.memory.retrieve_context("F1", "wheat query")
        self.assertEqual(len(ctx.relevant_interactions), 0)

    def test_no_history_farmer(self):
        ctx = self.memory.retrieve_context("NEW_FARMER", "any query")
        self.assertEqual(len(ctx.relevant_interactions), 0)
        self.assertIsNone(ctx.profile)

    def test_empty_query(self):
        self.memory.log_interaction("F1", "wheat query", "answer")
        ctx = self.memory.retrieve_context("F1", "")
        self.assertIsInstance(ctx.relevant_interactions, list)

    def test_missing_metadata(self):
        self.memory.log_interaction("F1", "generic question", "generic answer")
        ctx = self.memory.retrieve_context("F1", "generic question")
        self.assertIsInstance(ctx.relevant_interactions, list)

    def test_relevance_threshold_excludes_weak_matches(self):
        self.memory.log_interaction("F1", "wheat pest aphid yellow leaves", "answer")
        ctx = self.memory.retrieve_context("F1", "completely unrelated topic about payment processing delays")
        self.assertEqual(len(ctx.relevant_interactions), 0)

    def test_as_prompt_text_includes_profile(self):
        self.memory.log_interaction("F1", "wheat query", "answer", crop="Wheat")
        self.memory.consolidate_profile("F1")
        ctx = self.memory.retrieve_context("F1", "wheat query")
        text = ctx.as_prompt_text()
        self.assertIn("profile", text.lower())

    def test_thread_safety_smoke(self):
        import threading
        errors = []

        def worker(i):
            try:
                self.memory.log_interaction(f"F{i}", "query", "answer")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
