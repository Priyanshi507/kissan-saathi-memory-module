import unittest
import xml.etree.ElementTree as ET

from farmer_memory import SQLiteStorage
from procurement import ivr
from procurement.store import ProcurementStore


class TestSignature(unittest.TestCase):
    PARAMS = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    URL = "https://mycompany.com/myapp.php?foo=1&bar=2"

    def test_matches_the_value_published_in_twilios_documentation(self):
        self.assertEqual(ivr.compute_signature("12345", self.URL, self.PARAMS), "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")

    def test_valid_and_invalid(self):
        good = ivr.compute_signature("tok", self.URL, self.PARAMS)
        self.assertTrue(ivr.valid_signature("tok", self.URL, self.PARAMS, good))
        self.assertFalse(ivr.valid_signature("other", self.URL, self.PARAMS, good))
        self.assertFalse(ivr.valid_signature("tok", self.URL, {**self.PARAMS, "Digits": "9999"}, good))
        self.assertFalse(ivr.valid_signature("tok", self.URL, self.PARAMS, None))
        self.assertFalse(ivr.valid_signature("tok", self.URL, self.PARAMS, ""))

    def test_default_port_is_tolerated_both_ways(self):
        sig = ivr.compute_signature("tok", "https://x.example.com:443/ivr/voice", {"a": "1"})
        self.assertTrue(ivr.valid_signature("tok", "https://x.example.com/ivr/voice", {"a": "1"}, sig))
        sig2 = ivr.compute_signature("tok", "http://x.example.com/ivr/voice", {"a": "1"})
        self.assertTrue(ivr.valid_signature("tok", "http://x.example.com:80/ivr/voice", {"a": "1"}, sig2))

    def test_a_different_path_is_rejected(self):
        sig = ivr.compute_signature("tok", "https://x.example.com/ivr/voice", {})
        self.assertFalse(ivr.valid_signature("tok", "https://x.example.com/ivr/gather", {}, sig))


class TestHelpers(unittest.TestCase):
    def test_caller_to_farmer_id(self):
        self.assertEqual(ivr.farmer_id_from_caller("+919469455964"), "9469455964")
        self.assertEqual(ivr.farmer_id_from_caller("09469455964"), "9469455964")
        for bad in (None, "", "anonymous", "+12345"):
            self.assertIsNone(ivr.farmer_id_from_caller(bad))

    def test_language_mapping(self):
        self.assertEqual(ivr.spoken_lang("hi"), "hi-IN")
        self.assertEqual(ivr.spoken_lang("mr"), "mr-IN")
        self.assertEqual(ivr.spoken_lang("en-IN"), "en-IN")
        self.assertEqual(ivr.spoken_lang(None, "hi-IN"), "hi-IN")
        self.assertEqual(ivr.spoken_lang("xx", "hi-IN"), "hi-IN")


class TestTwiml(unittest.TestCase):
    def parse(self, xml):
        return ET.fromstring(xml)

    def test_output_is_well_formed_and_escapes_dangerous_text(self):
        xml = ivr.say_and_gather('Rain <b> & "hail"', "More?", "https://x/ivr/gather?a=1&b=2", "en-IN", "Bye")
        root = self.parse(xml)
        self.assertEqual(root.find("Say").text, 'Rain <b> & "hail"')
        self.assertEqual(root.find("Gather").get("action"), "https://x/ivr/gather?a=1&b=2")

    def test_gather_uses_speech_and_language(self):
        gather = self.parse(ivr.gather_response("Speak", "https://x/g", "hi-IN", "Bye")).find("Gather")
        self.assertEqual((gather.get("input"), gather.get("language"), gather.get("method")), ("speech", "hi-IN", "POST"))

    def test_hangup_and_length_cap(self):
        root = self.parse(ivr.say_and_hangup("x" * 5000, "en-IN"))
        self.assertIsNotNone(root.find("Hangup"))
        self.assertLessEqual(len(root.find("Say").text), ivr.MAX_SPOKEN_CHARS)

    def test_language_attribute_cannot_break_out_of_the_xml(self):
        root = self.parse(ivr.gather_response("Hi", "https://x/g", 'en" injected="1', "Bye"))
        self.assertIsNone(root.find("Gather").get("injected"))


class TestRecordsFirst(unittest.TestCase):
    def setUp(self):
        self.store = ProcurementStore(SQLiteStorage(":memory:"))
        self.store.upsert_mandi("m1", "Noida Mandi")

    def test_no_data(self):
        self.assertIn("do not see any payment", ivr.answer_from_records(self.store, "F1", "my payment?", "en"))
        self.assertIn("कोई भुगतान", ivr.answer_from_records(self.store, "F1", "मेरा भुगतान", "hi"))
        self.assertIn("do not see an upcoming slot", ivr.answer_from_records(self.store, "F1", "my slot", "en"))

    def test_payment_states_are_read_from_records(self):
        p = self.store.create_payment("F1", "Wheat", 10, 24250, sold_on="2026-09-16")
        self.assertIn("24,250 rupees", ivr.answer_from_records(self.store, "F1", "where is my money payment", "en"))
        self.store.update_payment(p["id"], "failed", "name_mismatch")
        reply = ivr.answer_from_records(self.store, "F1", "payment", "en")
        self.assertIn("Failed", reply)
        self.assertIn("Name does not match exactly", reply)
        self.store.update_payment(p["id"], "paid")
        self.assertIn("Paid", ivr.answer_from_records(self.store, "F1", "payment", "en"))

    def test_long_wait_mentions_complaint_option(self):
        self.store.now = lambda: __import__("datetime").datetime(2026, 9, 30)
        self.store.create_payment("F1", "Wheat", 10, None, sold_on="2026-09-16")
        reply = ivr.answer_from_records(self.store, "F1", "payment", "en")
        self.assertIn("14 days", reply)
        self.assertIn("complaint", reply)
        self.assertNotIn("rupees", reply)

    def test_hindi_payment_answer(self):
        self.store.create_payment("F1", "Wheat", 10, 24250)
        reply = ivr.answer_from_records(self.store, "F1", "मेरा पैसा", "hi")
        self.assertIn("24,250", reply)
        self.assertIn("लंबित", reply)

    def test_queue_answer(self):
        self.store.book_slot("c1", "F1", "TK", "m1", "Wheat", 5, "2026-09-16", "W")
        self.store.book_slot("c2", "F2", "TK", "m1", "Wheat", 5, "2026-09-16", "W")
        reply = ivr.answer_from_records(self.store, "F2", "what is my queue number", "en")
        self.assertIn("number 2", reply)
        self.assertIn("Noida Mandi", reply)

    def test_other_questions_fall_through_to_the_model(self):
        self.assertIsNone(ivr.answer_from_records(self.store, "F1", "how do I control aphids on wheat", "en"))
        self.assertIsNone(ivr.answer_from_records(self.store, "F1", "", "en"))


if __name__ == "__main__":
    unittest.main()
