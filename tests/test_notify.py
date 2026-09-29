import base64
import json
import unittest
from urllib.parse import parse_qs

from farmer_memory import SQLiteStorage
from procurement.notify import (
    LogSms, NotificationService, TEMPLATES, TwilioSms, phone_e164, render, sms_from_env,
)
from procurement.store import ProcurementStore


class TestRender(unittest.TestCase):
    def test_every_template_has_both_languages_and_same_placeholders(self):
        import string
        fields = lambda text: {f for _, f, _, _ in string.Formatter().parse(text) if f}
        for kind, langs in TEMPLATES.items():
            self.assertEqual(set(langs), {"en", "hi"}, kind)
            self.assertEqual(fields(langs["en"][1]), fields(langs["hi"][1]), kind)

    def test_render_fills_fields_and_falls_back(self):
        title, body = render("slot_booked", "en", {"mandi": "Noida", "date": "2026-09-16", "window": "9-12", "token": "TK-1"})
        self.assertEqual(title, "Slot confirmed")
        self.assertIn("Noida", body)
        self.assertEqual(render("slot_booked", "mr", {"mandi": "x"})[0], "Slot confirmed")
        self.assertIn("स्लॉट", render("slot_booked", "hi-IN", {})[0])

    def test_missing_fields_render_blank_not_crash(self):
        _, body = render("payment_update", "en", {"crop": "Wheat", "status": "Paid"})
        self.assertNotIn("{", body)
        self.assertEqual(body, body.strip())

    def test_unknown_kind_raises(self):
        with self.assertRaises(KeyError):
            render("nope", "en", {})


class TestPhone(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(phone_e164("9469455964"), "+919469455964")
        self.assertEqual(phone_e164("94694-55964"), "+919469455964")
        self.assertEqual(phone_e164("+919469455964"), "+919469455964")

    def test_not_a_phone(self):
        for value in ("F-123", "12345", "", None, "94694559641234"):
            self.assertIsNone(phone_e164(value), value)


class FakeTransport:
    def __init__(self, status=201, body='{"sid": "SM123"}', boom=None):
        self.status, self.body, self.boom, self.calls = status, body, boom, []

    def __call__(self, url, headers, data, timeout):
        self.calls.append((url, headers, data, timeout))
        if self.boom:
            raise self.boom
        return self.status, self.body


class TestTwilioSms(unittest.TestCase):
    def test_request_is_formed_correctly(self):
        transport = FakeTransport()
        status, detail = TwilioSms("ACx", "tok", from_number="+15550001", transport=transport).send("+919469455964", "Hello")
        self.assertEqual((status, detail), ("sent", "SM123"))
        url, headers, data, _ = transport.calls[0]
        self.assertEqual(url, "https://api.twilio.com/2010-04-01/Accounts/ACx/Messages.json")
        self.assertEqual(headers["Authorization"], "Basic " + base64.b64encode(b"ACx:tok").decode())
        self.assertEqual(parse_qs(data.decode()), {"To": ["+919469455964"], "Body": ["Hello"], "From": ["+15550001"]})

    def test_messaging_service_replaces_from(self):
        transport = FakeTransport()
        TwilioSms("ACx", "tok", messaging_service_sid="MG1", transport=transport).send("+91", "Hi")
        form = parse_qs(transport.calls[0][2].decode())
        self.assertEqual(form["MessagingServiceSid"], ["MG1"])
        self.assertNotIn("From", form)

    def test_unicode_body_survives_encoding(self):
        transport = FakeTransport()
        TwilioSms("ACx", "tok", from_number="+1", transport=transport).send("+91", "आपका स्लॉट पक्का हुआ")
        self.assertEqual(parse_qs(transport.calls[0][2].decode())["Body"], ["आपका स्लॉट पक्का हुआ"])

    def test_http_error_reports_twilio_message(self):
        transport = FakeTransport(status=400, body=json.dumps({"message": "The number is unverified"}))
        status, detail = TwilioSms("ACx", "tok", from_number="+1", transport=transport).send("+91", "x")
        self.assertEqual(status, "failed")
        self.assertIn("400", detail)
        self.assertIn("unverified", detail)

    def test_network_exception_becomes_failed_not_a_crash(self):
        status, detail = TwilioSms("ACx", "tok", from_number="+1", transport=FakeTransport(boom=TimeoutError("slow"))).send("+91", "x")
        self.assertEqual(status, "failed")
        self.assertIn("TimeoutError", detail)

    def test_non_json_success_body_is_still_sent(self):
        transport = FakeTransport(body="ok")
        self.assertEqual(TwilioSms("ACx", "tok", from_number="+1", transport=transport).send("+91", "x")[0], "sent")

    def test_requires_a_sender(self):
        with self.assertRaises(ValueError):
            TwilioSms("ACx", "tok")

    def test_env_selection(self):
        self.assertIsInstance(sms_from_env({}), LogSms)
        self.assertIsInstance(sms_from_env({"TWILIO_ACCOUNT_SID": "AC", "TWILIO_AUTH_TOKEN": "t"}), LogSms)
        self.assertIsInstance(sms_from_env({"TWILIO_ACCOUNT_SID": "AC", "TWILIO_AUTH_TOKEN": "t", "TWILIO_FROM": "+1"}), TwilioSms)


class TestService(unittest.TestCase):
    def setUp(self):
        self.store = ProcurementStore(SQLiteStorage(":memory:"))

    def rows(self):
        return self.store.admin_notifications()

    def test_log_mode_records_message_and_marks_it_logged(self):
        NotificationService(self.store, LogSms()).notify("9469455964", "complaint_registered", ref="#7")
        row = self.rows()[0]
        self.assertEqual(row["sms_status"], "logged")
        self.assertIn("#7", row["body"])
        self.assertEqual(len(self.store.farmer_notifications("9469455964")), 1)

    def test_farmer_language_preference_is_used(self):
        self.store.set_language("9469455964", "hi")
        NotificationService(self.store, LogSms()).notify("9469455964", "complaint_registered", ref="#7")
        self.assertIn("शिकायत", self.rows()[0]["title"])

    def test_explicit_language_overrides_preference(self):
        self.store.set_language("9469455964", "hi")
        NotificationService(self.store, LogSms()).notify("9469455964", "complaint_registered", language="en", ref="#7")
        self.assertEqual(self.rows()[0]["title"], "Complaint registered")

    def test_non_phone_farmer_id_still_gets_an_inbox_message_but_no_sms(self):
        transport = FakeTransport()
        service = NotificationService(self.store, TwilioSms("AC", "t", from_number="+1", transport=transport), background=False)
        service.notify("F-123", "complaint_registered", ref="#1")
        self.assertEqual(self.rows()[0]["sms_status"], "no_phone")
        self.assertEqual(transport.calls, [])

    def test_twilio_success_and_failure_are_recorded(self):
        ok = NotificationService(self.store, TwilioSms("AC", "t", from_number="+1", transport=FakeTransport()), background=False)
        ok.notify("9469455964", "complaint_registered", ref="#1")
        bad = NotificationService(self.store, TwilioSms("AC", "t", from_number="+1", transport=FakeTransport(status=500, body="down")), background=False)
        bad.notify("9469455964", "complaint_registered", ref="#2")
        statuses = [r["sms_status"] for r in reversed(self.rows())]
        self.assertEqual(statuses, ["sent", "failed"])


if __name__ == "__main__":
    unittest.main()
