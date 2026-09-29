import unittest

from procurement import payment_kb as kb
from procurement.complaint import build_complaint, grievance_label, status_label


class TestKnowledgeBase(unittest.TestCase):
    def test_every_code_has_complete_english_and_hindi_advice(self):
        for code in kb.CODES:
            for lang in ("en", "hi"):
                entry = kb.KB[code][lang]
                self.assertTrue(entry["title"] and entry["cause"], (code, lang))
                self.assertGreaterEqual(len(entry["steps"]), 2, (code, lang))
                self.assertTrue(entry["carry"], (code, lang))

    def test_english_and_hindi_have_matching_step_counts(self):
        for code in kb.CODES:
            self.assertEqual(len(kb.KB[code]["en"]["steps"]), len(kb.KB[code]["hi"]["steps"]), code)

    def test_advice_never_contains_invented_phone_numbers_or_urls(self):
        import re
        for code in kb.CODES:
            for lang in ("en", "hi"):
                blob = " ".join(kb.KB[code][lang]["steps"] + [kb.KB[code][lang]["cause"]])
                self.assertIsNone(re.search(r"\d{6,}|https?://|www\.", blob), (code, lang))

    def test_official_channels_are_the_only_named_sites(self):
        self.assertEqual({c["site"] for c in kb.OFFICIAL_CHANNELS}, {"pfms.nic.in", "pgportal.gov.in"})


class TestClassifyText(unittest.TestCase):
    def test_specific_causes_win_over_the_broad_aadhaar_keyword(self):
        self.assertEqual(kb.classify_text("Aadhaar name mismatch with bank"), "name_mismatch")
        self.assertEqual(kb.classify_text("aadhaar mapped to another bank"), "other_bank_mapped")

    def test_each_cause_is_recognised(self):
        cases = {
            "Aadhaar is not seeded with NPCI": "aadhaar_not_seeded",
            "invalid account number or IFSC": "wrong_account_or_ifsc",
            "account is dormant": "dormant_or_closed_account",
            "gate pass verification pending": "record_verification_pending",
            "payment under processing": "processing_delay",
        }
        for text, code in cases.items():
            self.assertEqual(kb.classify_text(text), code, text)

    def test_hindi_input(self):
        self.assertEqual(kb.classify_text("आधार सीडिंग नहीं हुई"), "aadhaar_not_seeded")
        self.assertEqual(kb.classify_text("नाम मेल नहीं खाता"), "name_mismatch")
        self.assertEqual(kb.classify_text("खाता बंद है"), "dormant_or_closed_account")

    def test_no_match_returns_none(self):
        for text in (None, "", "   ", "hello there", "what is the weather"):
            self.assertIsNone(kb.classify_text(text))


class TestExplain(unittest.TestCase):
    def test_language_and_fallbacks(self):
        self.assertEqual(kb.explain("name_mismatch", "hi")["language"], "hi")
        self.assertEqual(kb.explain("name_mismatch", "hi-IN")["language"], "hi")
        self.assertEqual(kb.explain("name_mismatch", "mr")["language"], "en")
        self.assertEqual(kb.explain("name_mismatch", None)["language"], "en")

    def test_unknown_or_missing_code_falls_back_safely(self):
        self.assertEqual(kb.explain("made_up")["code"], "unknown")
        self.assertEqual(kb.explain(None)["code"], "unknown")

    def test_parse_code_only_accepts_known_specific_codes(self):
        self.assertEqual(kb.parse_code('{"code": "name_mismatch"}'), "name_mismatch")
        self.assertEqual(kb.parse_code('```json\n{"code": "dormant_or_closed_account"}\n```'), "dormant_or_closed_account")
        for bad in (None, "", "nonsense", '{"code": "made_up"}', '{"code": "unknown"}', "[1]", '{"code": 5}'):
            self.assertIsNone(kb.parse_code(bad), bad)

    def test_classifier_prompt_lists_every_actionable_code(self):
        prompt = kb.classifier_prompt()
        for code in kb.CODES:
            self.assertIn(code, prompt)


PAYMENT = {
    "crop": "Wheat", "quantity_qtl": 10.0, "amount": 24250.0, "sold_on": "2026-09-16",
    "status": "failed", "failure_code": "name_mismatch", "days_waiting": 9, "mandi_name": "Noida Mandi",
    "timeline": [
        {"ts": "2026-09-16T08:00:00", "status": "pending", "failure_code": None, "note": None},
        {"ts": "2026-09-20T10:00:00", "status": "failed", "failure_code": "name_mismatch", "note": "Bank rejected"},
    ],
}


class TestComplaint(unittest.TestCase):
    def test_english_contains_every_fact(self):
        text = build_complaint(PAYMENT, "9469455964", "en")
        for fact in ("9469455964", "Wheat, 10 quintal", "Noida Mandi", "2026-09-16", "Rs 24,250", "Failed", "9 days",
                     "Name does not match exactly", "2026-09-20: Failed (Bank rejected)"):
            self.assertIn(fact, text)

    def test_hindi_version(self):
        text = build_complaint(PAYMENT, "9469455964", "hi")
        for fact in ("9469455964", "क्विंटल", "Noida Mandi", "विफल", "नाम बिल्कुल मेल नहीं खाता"):
            self.assertIn(fact, text)

    def test_unsupported_language_falls_back_to_english(self):
        self.assertTrue(build_complaint(PAYMENT, "1", "mr").startswith("Subject:"))

    def test_missing_optional_values_do_not_break_or_invent_data(self):
        bare = {**PAYMENT, "amount": None, "quantity_qtl": None, "mandi_name": None, "failure_code": None}
        text = build_complaint(bare, "1", "en")
        self.assertIn("Amount expected: -", text)
        self.assertIn("Procurement centre: -", text)
        self.assertIn("Reason recorded: -", text)

    def test_labels(self):
        self.assertEqual(status_label("paid", "hi"), "भुगतान हो गया")
        self.assertEqual(status_label("weird", "en"), "weird")
        self.assertEqual(grievance_label("in_progress", "hi"), "कार्रवाई जारी है")


if __name__ == "__main__":
    unittest.main()
