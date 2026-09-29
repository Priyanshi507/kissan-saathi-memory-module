import base64
import binascii
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from procurement import payment_kb
from procurement.complaint import build_complaint
from procurement.store import InvalidTransition, NotFound, WindowFull

from .deps import Deps


class SlotBookingRequest(BaseModel):
    client_id: str
    farmer_id: str
    token: str
    mandi_id: str
    mandi_name: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    crop: Optional[str] = None
    quantity_qtl: Optional[float] = None
    date: str
    window: str


class SlotCancelRequest(BaseModel):
    farmer_id: str


class DiagnoseRequest(BaseModel):
    text: Optional[str] = None
    image_base64: Optional[str] = None
    mime_type: str = "image/jpeg"
    language: Optional[str] = None


class EscalateRequest(BaseModel):
    language: Optional[str] = None


class PrefsRequest(BaseModel):
    language: str


class ProfileRequest(BaseModel):
    name: str
    primary_crops: List[str] = []
    district: Optional[str] = None
    state: Optional[str] = None


def _clean_place(value: Optional[str]) -> Optional[str]:
    cleaned = " ".join(value.split())[:80] if value else ""
    return cleaned or None


def build(deps: Deps) -> APIRouter:
    router = APIRouter()
    store, memory, notifications = deps.store, deps.memory, deps.notifications

    def payment_for(farmer_id: str, payment_id: int) -> Dict[str, Any]:
        try:
            payment = store.get_payment(payment_id)
        except NotFound:
            raise HTTPException(404, "Payment not found")
        if payment["farmer_id"] != farmer_id:
            raise HTTPException(404, "Payment not found")
        return payment

    def with_complaint(payment: Dict[str, Any]) -> Dict[str, Any]:
        grievance = memory.get_grievance(payment["grievance_id"]) if payment.get("grievance_id") else None
        payment["complaint"] = (
            {"id": grievance["id"], "status": grievance["grievance_status"], "note": grievance["grievance_note"]}
            if grievance else None
        )
        return payment

    @router.post("/slots")
    def book_slot(req: SlotBookingRequest):
        try:
            store.upsert_mandi(req.mandi_id, req.mandi_name, req.latitude, req.longitude)
            slot = store.book_slot(req.client_id, req.farmer_id, req.token, req.mandi_id,
                                   req.crop, req.quantity_qtl, req.date, req.window)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        except WindowFull:
            notifications.notify(req.farmer_id, "slot_rejected", mandi=req.mandi_name, date=req.date)
            raise HTTPException(409, "window_full")
        if slot["created"]:
            notifications.notify(req.farmer_id, "slot_booked", mandi=req.mandi_name,
                                 date=slot["slot_date"], window=slot["window"], token=slot["token"])
        return slot

    @router.post("/slots/{client_id}/cancel")
    def cancel_slot(client_id: str, req: SlotCancelRequest):
        try:
            return store.cancel_slot(client_id, req.farmer_id)
        except NotFound:
            raise HTTPException(404, "Slot not found")

    @router.get("/farmer/{farmer_id}/slots")
    def farmer_slots(farmer_id: str):
        return store.farmer_slots(farmer_id)

    @router.get("/farmer/{farmer_id}/payments")
    def farmer_payments(farmer_id: str):
        return [with_complaint(p) for p in store.farmer_payments(farmer_id)]

    @router.post("/farmer/{farmer_id}/payments/{payment_id}/diagnose")
    def diagnose(farmer_id: str, payment_id: int, req: DiagnoseRequest):
        payment = payment_for(farmer_id, payment_id)
        text = (req.text or "").strip() or None
        image_bytes = None
        if req.image_base64:
            try:
                image_bytes = base64.b64decode(req.image_base64, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(422, "image_base64 is not valid base64")

        code, source = None, None
        if payment["status"] == "failed" and payment.get("failure_code"):
            code, source = payment["failure_code"], "centre_record"
        if not code and text:
            code = payment_kb.classify_text(text)
            source = "your_message" if code else None
        if not code and deps.classify_issue and (text or image_bytes):
            try:
                code = deps.classify_issue(text, image_bytes, req.mime_type)
            except Exception:
                code = None
            source = "ai_reading" if code else None
        if not code:
            provided = bool(text or image_bytes)
            if payment["status"] in ("pending", "processing") and not provided:
                code, source = "processing_delay", "status"
            else:
                code, source = "unknown", "status"

        return {
            "payment_id": payment_id,
            "source": source,
            "photo_needs_ai": bool(image_bytes and not deps.classify_issue),
            "can_escalate": payment["can_escalate"],
            "diagnosis": payment_kb.explain(code, req.language or store.get_language(farmer_id)),
        }

    @router.post("/farmer/{farmer_id}/payments/{payment_id}/escalate")
    def escalate(farmer_id: str, payment_id: int, req: EscalateRequest):
        payment = payment_for(farmer_id, payment_id)
        language = req.language or store.get_language(farmer_id)
        if payment.get("grievance_id"):
            grievance = memory.get_grievance(payment["grievance_id"])
            return {"grievance_id": payment["grievance_id"], "already_registered": True,
                    "complaint_text": grievance["query_text"] if grievance else None}
        if not payment["can_escalate"]:
            raise HTTPException(409, "not_yet")

        complaint = build_complaint(payment, farmer_id, language)
        grievance_id = memory.log_interaction(
            farmer_id=farmer_id, query_text=complaint,
            response_text="Complaint registered from Where's my money. Awaiting officer review.",
            crop=payment.get("crop"), intent="payment_escalation", is_grievance=True, embed=False)
        store.link_grievance(payment_id, grievance_id)
        notifications.notify(farmer_id, "complaint_registered", language=language, ref=f"#{grievance_id}")
        return {"grievance_id": grievance_id, "already_registered": False, "complaint_text": complaint}

    @router.get("/farmer/{farmer_id}/grievances")
    def farmer_grievances(farmer_id: str):
        return [
            {"id": g["id"], "ts": g["ts"], "text": g["query_text"], "status": g["grievance_status"], "note": g["grievance_note"]}
            for g in memory.farmer_grievances(farmer_id)
        ]

    @router.get("/farmer/{farmer_id}/notifications")
    def farmer_notifications(farmer_id: str, limit: int = 50):
        return store.farmer_notifications(farmer_id, min(max(limit, 1), 200))

    @router.post("/farmer/{farmer_id}/notifications/read")
    def mark_read(farmer_id: str):
        return {"marked": store.mark_notifications_read(farmer_id)}

    @router.put("/farmer/{farmer_id}/prefs")
    def set_prefs(farmer_id: str, req: PrefsRequest):
        store.set_language(farmer_id, req.language.split("-")[0].lower())
        return {"language": store.get_language(farmer_id)}

    @router.put("/farmer/{farmer_id}/profile")
    def put_profile(farmer_id: str, req: ProfileRequest):
        name = " ".join(req.name.split())
        if not name or len(name) > 100:
            raise HTTPException(422, "name must be 1-100 characters")
        crops = [c.strip() for c in req.primary_crops if c and c.strip()][:20]
        if any(len(c) > 40 for c in crops):
            raise HTTPException(422, "each crop must be at most 40 characters")
        return store.upsert_farmer(farmer_id, name, crops, _clean_place(req.district), _clean_place(req.state))

    @router.delete("/farmer/{farmer_id}")
    def forget(farmer_id: str):
        """DPDP Act right to erasure: removes chat history, slots, payments, messages and registration."""
        deleted = memory.forget_farmer(farmer_id)
        erased = store.erase_farmer(farmer_id)
        return {"farmer_id": farmer_id, "rows_deleted": deleted, "erased": erased}

    return router
