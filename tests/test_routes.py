import base64
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from farmer_memory import FarmerMemoryModule, SQLiteStorage
from procurement import ivr
from procurement.notify import LogSms, NotificationService
from procurement.store import ProcurementStore
from routes import farmer_routes, ivr_routes, staff_routes
from routes.deps import Deps

TOKEN = "staff-secret"
WINDOW = "08:30 AM - 11:30 AM"
FARMER = "9469455964"
STAFF = {"X-Admin-Token": TOKEN}


def check_admin(value):
    if value != TOKEN:
        raise HTTPException(401, "Invalid or missing admin token")


def make_client(**overrides):
    memory = FarmerMemoryModule(storage=SQLiteStorage(":memory:"), embed_fn=lambda text: [0.0])
    store = ProcurementStore(memory.storage, now=lambda: datetime(2026, 9, 16, 9, 0, 0))
    deps = Deps(memory=memory, store=store, notifications=NotificationService(store, LogSms()), check_admin=check_admin)
    for key, value in overrides.items():
        setattr(deps, key, value)
    app = FastAPI()
    for module in (farmer_routes, staff_routes, ivr_routes):
        app.include_router(module.build(deps))
    return TestClient(app), deps


def booking(**kw):
    body = {"client_id": "c1", "farmer_id": FARMER, "token": "TK-1", "mandi_id": "m1", "mandi_name": "Noida Mandi",
            "latitude": 28.5, "longitude": 77.4, "crop": "Wheat", "quantity_qtl": 10, "date": "16 Sep 2026", "window": WINDOW}
    body.update(kw)
    return body


class TestFarmerJourney(unittest.TestCase):
    def setUp(self):
        self.client, self.deps = make_client()

    def test_slot_booking_is_idempotent_and_notifies_once(self):
        first = self.client.post("/slots", json=booking())
        again = self.client.post("/slots", json=booking())
        self.assertEqual((first.status_code, again.status_code), (200, 200))
        self.assertEqual(first.json()["id"], again.json()["id"])
        self.assertEqual(len(self.client.get(f"/farmer/{FARMER}/notifications").json()), 1)
        slots = self.client.get(f"/farmer/{FARMER}/slots").json()
        self.assertEqual((len(slots), slots[0]["queue"]["position"]), (1, 1))
        self.assertEqual(slots[0]["latitude"], 28.5)

    def test_full_window_returns_409_and_leaves_an_inbox_explanation(self):
        self.deps.store.upsert_mandi("m1", "Noida Mandi")
        self.deps.store.conn.execute("UPDATE mandis SET capacity_per_window = 1 WHERE id = 'm1'")
        self.client.post("/slots", json=booking())
        second = self.client.post("/slots", json=booking(client_id="c2", farmer_id="9111111111"))
        self.assertEqual(second.status_code, 409)
        inbox = self.client.get("/farmer/9111111111/notifications").json()
        self.assertEqual(inbox[0]["title"], "Slot could not be confirmed")

    def test_bad_date_is_422(self):
        self.assertEqual(self.client.post("/slots", json=booking(date="someday")).status_code, 422)

    def test_cancel_frees_the_queue(self):
        self.client.post("/slots", json=booking())
        self.assertEqual(self.client.post("/slots/c1/cancel", json={"farmer_id": FARMER}).json()["status"], "cancelled")
        self.assertEqual(self.client.post("/slots/nope/cancel", json={"farmer_id": FARMER}).status_code, 404)

    def test_whole_journey_slot_to_paid_with_a_failure_and_complaint(self):
        self.client.post("/slots", json=booking())
        slot_id = self.client.get("/admin/slots", headers=STAFF).json()[0]["id"]

        checked = self.client.patch(f"/admin/slots/{slot_id}", json={"status": "checked_in"}, headers=STAFF)
        self.assertEqual(checked.status_code, 200)
        done = self.client.patch(f"/admin/slots/{slot_id}", json={"status": "completed", "amount": 24250}, headers=STAFF)
        payment_id = done.json()["payment_id"]

        payments = self.client.get(f"/farmer/{FARMER}/payments").json()
        self.assertEqual((payments[0]["status"], payments[0]["amount"], payments[0]["mandi_name"]), ("pending", 24250, "Noida Mandi"))

        bad_code = self.client.patch(f"/admin/payments/{payment_id}", json={"status": "failed", "failure_code": "bogus"}, headers=STAFF)
        self.assertEqual(bad_code.status_code, 422)
        self.client.patch(f"/admin/payments/{payment_id}", json={"status": "failed", "failure_code": "name_mismatch"}, headers=STAFF)

        diagnosis = self.client.post(f"/farmer/{FARMER}/payments/{payment_id}/diagnose", json={"language": "hi"}).json()
        self.assertEqual(diagnosis["source"], "centre_record")
        self.assertEqual(diagnosis["diagnosis"]["code"], "name_mismatch")
        self.assertEqual(diagnosis["diagnosis"]["language"], "hi")
        self.assertTrue(diagnosis["can_escalate"])

        esc = self.client.post(f"/farmer/{FARMER}/payments/{payment_id}/escalate", json={"language": "en"}).json()
        self.assertFalse(esc["already_registered"])
        self.assertIn("Rs 24,250", esc["complaint_text"])
        again = self.client.post(f"/farmer/{FARMER}/payments/{payment_id}/escalate", json={}).json()
        self.assertTrue(again["already_registered"])
        self.assertEqual(again["grievance_id"], esc["grievance_id"])

        complaints = self.client.get(f"/farmer/{FARMER}/grievances").json()
        self.assertEqual((len(complaints), complaints[0]["status"]), (1, "new"))
        self.assertEqual(self.deps.memory.admin_grievances()[0]["id"], esc["grievance_id"])
        listed = self.client.get(f"/farmer/{FARMER}/payments").json()[0]
        self.assertEqual(listed["complaint"]["id"], esc["grievance_id"])
        self.assertFalse(listed["can_escalate"])

        titles = [n["title"] for n in self.client.get(f"/farmer/{FARMER}/notifications").json()]
        for expected in ("Slot confirmed", "You are in the queue", "Produce received", "Payment update", "Complaint registered"):
            self.assertIn(expected, titles)

    def test_escalating_too_early_is_refused(self):
        payment = self.client.post("/admin/payments", json={"farmer_id": FARMER, "crop": "Wheat", "sold_on": "2026-09-15"}, headers=STAFF).json()
        response = self.client.post(f"/farmer/{FARMER}/payments/{payment['id']}/escalate", json={})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "not_yet"))

    def test_a_farmer_cannot_touch_someone_elses_payment(self):
        payment = self.client.post("/admin/payments", json={"farmer_id": FARMER, "crop": "Wheat"}, headers=STAFF).json()
        for path in ("diagnose", "escalate"):
            response = self.client.post(f"/farmer/9000000000/payments/{payment['id']}/{path}", json={})
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.client.get("/farmer/9000000000/payments").json(), [])

    def test_language_preference_changes_later_messages(self):
        self.client.put(f"/farmer/{FARMER}/prefs", json={"language": "hi-IN"})
        self.client.post("/slots", json=booking())
        self.assertIn("स्लॉट", self.client.get(f"/farmer/{FARMER}/notifications").json()[0]["title"])

    def test_inbox_can_be_marked_read(self):
        self.client.post("/slots", json=booking())
        self.assertEqual(self.client.post(f"/farmer/{FARMER}/notifications/read").json()["marked"], 1)
        self.assertIsNotNone(self.client.get(f"/farmer/{FARMER}/notifications").json()[0]["read_at"])


class TestDiagnosis(unittest.TestCase):
    def new_payment(self, client):
        return client.post("/admin/payments", json={"farmer_id": FARMER, "crop": "Wheat"}, headers=STAFF).json()["id"]

    def test_pending_payment_with_no_input_is_explained_as_processing(self):
        client, _ = make_client()
        result = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose", json={}).json()
        self.assertEqual((result["source"], result["diagnosis"]["code"]), ("status", "processing_delay"))

    def test_farmer_text_is_classified_by_keywords_without_any_ai(self):
        client, _ = make_client()
        result = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose",
                             json={"text": "bank says name does not match aadhaar"}).json()
        self.assertEqual((result["source"], result["diagnosis"]["code"]), ("your_message", "name_mismatch"))

    def test_image_is_read_by_the_classifier_when_available(self):
        seen = {}

        def classify(text, image, mime):
            seen.update(text=text, image=image, mime=mime)
            return "aadhaar_not_seeded"

        client, _ = make_client(classify_issue=classify)
        image = base64.b64encode(b"fake-image").decode()
        result = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose", json={"image_base64": image}).json()
        self.assertEqual((result["source"], result["diagnosis"]["code"]), ("ai_reading", "aadhaar_not_seeded"))
        self.assertEqual(seen["image"], b"fake-image")

    def test_image_without_ai_is_reported_honestly_not_guessed(self):
        client, _ = make_client()
        image = base64.b64encode(b"x").decode()
        result = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose", json={"image_base64": image}).json()
        self.assertTrue(result["photo_needs_ai"])
        self.assertEqual(result["diagnosis"]["code"], "unknown")

    def test_classifier_failure_degrades_to_unknown(self):
        def broken(*_):
            raise RuntimeError("quota")

        client, _ = make_client(classify_issue=broken)
        result = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose", json={"text": "hmm not sure"}).json()
        self.assertEqual(result["diagnosis"]["code"], "unknown")

    def test_staff_record_beats_the_farmers_guess(self):
        client, _ = make_client()
        pid = self.new_payment(client)
        client.patch(f"/admin/payments/{pid}", json={"status": "failed", "failure_code": "dormant_or_closed_account"}, headers=STAFF)
        result = client.post(f"/farmer/{FARMER}/payments/{pid}/diagnose", json={"text": "aadhaar not seeded"}).json()
        self.assertEqual((result["source"], result["diagnosis"]["code"]), ("centre_record", "dormant_or_closed_account"))

    def test_invalid_base64_is_422(self):
        client, _ = make_client()
        response = client.post(f"/farmer/{FARMER}/payments/{self.new_payment(client)}/diagnose", json={"image_base64": "%%%not-base64"})
        self.assertEqual(response.status_code, 422)


class TestStaffAuthAndRules(unittest.TestCase):
    def test_every_admin_route_requires_the_token(self):
        client, _ = make_client()
        for method, path in (("get", "/admin/slots"), ("get", "/admin/queue"), ("get", "/admin/payments"),
                             ("get", "/admin/notifications"), ("get", "/admin/failure-codes"),
                             ("patch", "/admin/slots/1"), ("patch", "/admin/payments/1"), ("post", "/admin/payments")):
            kwargs = {"json": {"status": "paid", "farmer_id": "x"}} if method != "get" else {}
            self.assertEqual(getattr(client, method)(path, **kwargs).status_code, 401, path)
            self.assertEqual(getattr(client, method)(path, headers={"X-Admin-Token": "wrong"}, **kwargs).status_code, 401, path)

    def test_illegal_slot_transitions_are_409_and_unknown_ids_404(self):
        client, _ = make_client()
        client.post("/slots", json=booking())
        sid = client.get("/admin/slots", headers=STAFF).json()[0]["id"]
        self.assertEqual(client.patch(f"/admin/slots/{sid}", json={"status": "completed"}, headers=STAFF).status_code, 409)
        self.assertEqual(client.patch("/admin/slots/999", json={"status": "checked_in"}, headers=STAFF).status_code, 404)
        self.assertEqual(client.patch("/admin/payments/999", json={"status": "paid"}, headers=STAFF).status_code, 404)
        self.assertEqual(client.patch("/admin/payments/1", json={"status": "teleported"}, headers=STAFF).status_code, 422)

    def test_queue_load_and_message_log(self):
        client, _ = make_client()
        client.post("/slots", json=booking()); client.post("/slots", json=booking(client_id="c2", farmer_id="9111111111"))
        load = client.get("/admin/queue?date=2026-09-16", headers=STAFF).json()
        self.assertEqual((load[0]["active"], load[0]["mandi_name"]), (2, "Noida Mandi"))
        log = client.get("/admin/notifications", headers=STAFF).json()
        self.assertEqual({row["sms_status"] for row in log}, {"logged"})

    def test_failure_code_list_is_available_for_the_dashboard_dropdown(self):
        client, _ = make_client()
        codes = {c["code"] for c in client.get("/admin/failure-codes", headers=STAFF).json()}
        self.assertIn("name_mismatch", codes)
        self.assertNotIn("unknown", codes)


class TestFarmerRegistry(unittest.TestCase):
    def setUp(self):
        self.client, self.deps = make_client()

    def test_profile_sync_stores_and_cleans_the_data(self):
        r = self.client.put(f"/farmer/{FARMER}/profile", json={"name": "  Priya   Sharma ", "primary_crops": ["Wheat", " ", "Paddy"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["name"], r.json()["primary_crops"]), ("Priya Sharma", ["Wheat", "Paddy"]))

    def test_profile_validation(self):
        for body in ({"name": "   "}, {"name": "x" * 101}, {"name": "A", "primary_crops": ["c" * 41]}):
            self.assertEqual(self.client.put(f"/farmer/{FARMER}/profile", json=body).status_code, 422, body)

    def test_registered_farmer_with_no_chat_shows_in_the_dashboard_list(self):
        self.client.put("/farmer/8882355656/profile", json={"name": "Priya", "primary_crops": ["Cotton"]})
        self.deps.memory.log_interaction("9469455964", "wheat rust?", "use fungicide", crop="Wheat")
        rows = self.client.get("/admin/farmers", headers=STAFF).json()
        by_id = {r["farmer_id"]: r for r in rows}
        self.assertEqual(set(by_id), {"8882355656", "9469455964"})
        self.assertEqual((by_id["8882355656"]["name"], by_id["8882355656"]["message_count"]), ("Priya", 0))
        self.assertEqual((by_id["9469455964"]["name"], by_id["9469455964"]["message_count"]), (None, 1))

    def test_search_by_name(self):
        self.client.put("/farmer/8882355656/profile", json={"name": "Priya", "primary_crops": []})
        self.client.put("/farmer/9111111111/profile", json={"name": "Ravi", "primary_crops": []})
        found = self.client.get("/admin/farmers?search=priy", headers=STAFF).json()
        self.assertEqual([r["farmer_id"] for r in found], ["8882355656"])

    def test_detail_for_a_registered_farmer_with_no_chat_is_not_a_404(self):
        self.client.put("/farmer/8882355656/profile", json={"name": "Priya", "primary_crops": ["Cotton"]})
        r = self.client.get("/admin/farmers/8882355656", headers=STAFF)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["name"], r.json()["interactions"], r.json()["primary_crops"]), ("Priya", [], ["Cotton"]))

    def test_each_farmers_detail_contains_only_their_own_chat(self):
        self.deps.memory.log_interaction("9111111111", "wheat rust question", "answer A", crop="Wheat")
        self.deps.memory.log_interaction("9222222222", "mustard aphids question", "answer B", crop="Mustard")
        a = self.client.get("/admin/farmers/9111111111", headers=STAFF).json()
        b = self.client.get("/admin/farmers/9222222222", headers=STAFF).json()
        self.assertEqual([i["query_text"] for i in a["interactions"]], ["wheat rust question"])
        self.assertEqual([i["query_text"] for i in b["interactions"]], ["mustard aphids question"])

    def test_unknown_farmer_detail_is_404_and_admin_token_is_required(self):
        self.assertEqual(self.client.get("/admin/farmers/0000000000", headers=STAFF).status_code, 404)
        self.assertEqual(self.client.get("/admin/farmers").status_code, 401)
        self.assertEqual(self.client.get("/admin/farmers/9111111111").status_code, 401)


class TestIvr(unittest.TestCase):
    BASE = "https://demo.example.com"

    def signed(self, client, path, params, token="twilio-token"):
        signature = ivr.compute_signature(token, self.BASE + path, params)
        return client.post(path, data=params, headers={"X-Twilio-Signature": signature})

    def test_unconfigured_ivr_refuses_rather_than_accepting_unsigned_calls(self):
        client, _ = make_client()
        self.assertEqual(client.post("/ivr/voice", data={"From": "+919469455964"}).status_code, 503)

    def test_signature_is_enforced(self):
        client, _ = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE)
        params = {"From": "+919469455964", "CallSid": "CA1"}
        self.assertEqual(client.post("/ivr/voice", data=params).status_code, 403)
        self.assertEqual(client.post("/ivr/voice", data=params, headers={"X-Twilio-Signature": "forged"}).status_code, 403)
        ok = self.signed(client, "/ivr/voice", params)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.headers["content-type"].split(";")[0], "application/xml")
        gather = ET.fromstring(ok.text).find("Gather")
        self.assertEqual(gather.get("action"), self.BASE + "/ivr/gather")
        self.assertEqual(gather.get("language"), "hi-IN")

    def test_a_valid_signature_cannot_be_replayed_with_different_parameters(self):
        client, _ = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE)
        signature = ivr.compute_signature("twilio-token", self.BASE + "/ivr/voice", {"From": "+919469455964"})
        tampered = client.post("/ivr/voice", data={"From": "+911111111111"}, headers={"X-Twilio-Signature": signature})
        self.assertEqual(tampered.status_code, 403)

    def test_gather_answers_payment_questions_from_records_and_never_calls_the_model(self):
        def must_not_be_called(*_):
            raise AssertionError("the model must not answer payment questions")

        client, deps = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE, answer=must_not_be_called)
        deps.store.create_payment(FARMER, "Wheat", 10, 24250)
        reply = self.signed(client, "/ivr/gather", {"From": "+919469455964", "SpeechResult": "my payment status"})
        say = ET.fromstring(reply.text).findall("Say")[0].text
        self.assertIn("24,250", say)

    def test_other_questions_go_to_the_model_and_failures_are_spoken_gracefully(self):
        client, _ = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE,
                                answer=lambda farmer, text, lang: f"Model says: {text}")
        reply = self.signed(client, "/ivr/gather", {"From": "+919469455964", "SpeechResult": "aphids on wheat"})
        self.assertIn("Model says: aphids on wheat", reply.text)

        def broken(*_):
            raise RuntimeError("quota")

        client, _ = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE, answer=broken)
        reply = self.signed(client, "/ivr/gather", {"From": "+919469455964", "SpeechResult": "aphids on wheat"})
        self.assertEqual(reply.status_code, 200)
        self.assertIn("Say", reply.text)

    def test_silence_reprompts_and_unknown_caller_hangs_up(self):
        client, _ = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE)
        silent = self.signed(client, "/ivr/gather", {"From": "+919469455964", "SpeechResult": ""})
        self.assertIsNotNone(ET.fromstring(silent.text).find("Gather"))
        anonymous = self.signed(client, "/ivr/gather", {"From": "anonymous", "SpeechResult": "hello"})
        self.assertIsNotNone(ET.fromstring(anonymous.text).find("Hangup"))

    def test_callers_saved_language_is_used(self):
        client, deps = make_client(ivr_auth_token="twilio-token", public_base_url=self.BASE)
        deps.store.set_language(FARMER, "mr")
        reply = self.signed(client, "/ivr/voice", {"From": "+919469455964"})
        self.assertEqual(ET.fromstring(reply.text).find("Gather").get("language"), "mr-IN")

    def test_unsigned_development_mode(self):
        client, _ = make_client(ivr_allow_unsigned=True)
        self.assertEqual(client.post("/ivr/voice", data={"From": "+919469455964"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
