from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from procurement import insights, payment_kb
from procurement.complaint import grievance_label, status_label
from procurement.registry import merge_farmers
from procurement.store import InvalidTransition, NotFound

from .deps import Deps


class SlotStatusUpdate(BaseModel):
    status: str
    amount: Optional[float] = None


class PaymentCreate(BaseModel):
    farmer_id: str
    crop: Optional[str] = None
    quantity_qtl: Optional[float] = None
    amount: Optional[float] = None
    sold_on: Optional[str] = None
    note: Optional[str] = None


class GrievanceUpdate(BaseModel):
    status: str
    note: Optional[str] = None


class MandiUpdate(BaseModel):
    capacity_per_window: int


class Announcement(BaseModel):
    title: str
    body: str
    farmer_id: Optional[str] = None


class PaymentUpdate(BaseModel):
    status: str
    failure_code: Optional[str] = None
    note: Optional[str] = None


def build(deps: Deps) -> APIRouter:
    router = APIRouter(prefix="/admin")
    store, notifications = deps.store, deps.notifications

    def admin(x_admin_token: Optional[str] = Header(None)):
        deps.check_admin(x_admin_token)

    @router.get("/slots", dependencies=[Depends(admin)])
    def slots(status: Optional[str] = None, date: Optional[str] = None, mandi_id: Optional[str] = None):
        try:
            return store.admin_slots(status, date, mandi_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.patch("/slots/{slot_id}", dependencies=[Depends(admin)])
    def update_slot(slot_id: int, req: SlotStatusUpdate):
        try:
            slot = store.set_slot_status(slot_id, req.status)
        except NotFound:
            raise HTTPException(404, "Slot not found")
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc))

        mandi = slot["mandi_name"] or slot["mandi_id"]
        if req.status == "checked_in":
            queue = slot["queue"] or {}
            notifications.notify(slot["farmer_id"], "slot_checked_in", mandi=mandi,
                                 position=queue.get("position", ""), wait=queue.get("est_wait_minutes", ""))
        elif req.status == "completed":
            payment = store.create_payment(slot["farmer_id"], slot["crop"], slot["quantity_qtl"], req.amount,
                                           sold_on=slot["slot_date"], slot_id=slot_id)
            slot["payment_id"] = payment["id"]
            notifications.notify(slot["farmer_id"], "slot_completed", mandi=mandi)
        return slot

    @router.get("/queue", dependencies=[Depends(admin)])
    def queue(date: Optional[str] = None):
        try:
            return store.window_load(date)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.get("/payments", dependencies=[Depends(admin)])
    def payments(status: Optional[str] = None):
        return store.admin_payments(status)

    @router.post("/payments", dependencies=[Depends(admin)])
    def create_payment(req: PaymentCreate):
        try:
            payment = store.create_payment(req.farmer_id, req.crop, req.quantity_qtl, req.amount, req.sold_on, note=req.note)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        notifications.notify(req.farmer_id, "payment_update", crop=req.crop or "",
                             status=status_label("pending", store.get_language(req.farmer_id)), detail="")
        return payment

    @router.patch("/payments/{payment_id}", dependencies=[Depends(admin)])
    def update_payment(payment_id: int, req: PaymentUpdate):
        if req.failure_code and req.failure_code not in payment_kb.CODES:
            raise HTTPException(422, f"failure_code must be one of {list(payment_kb.CODES)}")
        try:
            payment = store.update_payment(payment_id, req.status, req.failure_code, req.note)
        except NotFound:
            raise HTTPException(404, "Payment not found")
        except InvalidTransition as exc:
            raise HTTPException(422, str(exc))

        language = store.get_language(payment["farmer_id"])
        detail = payment_kb.explain(payment["failure_code"], language)["title"] if payment["failure_code"] else (req.note or "")
        notifications.notify(payment["farmer_id"], "payment_update", crop=payment["crop"] or "",
                             status=status_label(payment["status"], language), detail=detail)
        return payment

    @router.get("/notifications", dependencies=[Depends(admin)])
    def message_log(limit: int = 100):
        return store.admin_notifications(min(max(limit, 1), 500))

    def all_farmers(search: Optional[str] = None):
        return merge_farmers(deps.memory.admin_farmers(None), store.list_registered(), search,
                             store.all_languages(), insights.farmer_counts(store))

    @router.get("/farmers", dependencies=[Depends(admin)])
    def farmers(search: Optional[str] = None):
        return all_farmers(search)

    @router.get("/farmers/{farmer_id}", dependencies=[Depends(admin)])
    def farmer_detail(farmer_id: str):
        history = deps.memory.admin_farmer_history(farmer_id)
        registered = store.get_registered(farmer_id)
        if not history and not registered:
            raise HTTPException(404, "No interactions found for this farmer_id")
        profile = deps.memory.storage.get_profile(farmer_id)
        location = [p for p in ((registered or {}).get("district"), (registered or {}).get("state")) if p]
        return {
            "farmer_id": farmer_id,
            "name": registered["name"] if registered else None,
            "primary_crops": registered["primary_crops"] if registered else [],
            "registered_at": registered["registered_at"] if registered else None,
            "location": ", ".join(location) or None,
            "language": store.get_language(farmer_id),
            "interactions": history,
            "profile_summary": profile.summary_text if profile else None,
            "slots": store.farmer_slots(farmer_id, 50),
            "payments": store.farmer_payments(farmer_id),
            "complaints": deps.memory.farmer_grievances(farmer_id),
        }

    @router.get("/grievances", dependencies=[Depends(admin)])
    def grievances(status: Optional[str] = None):
        rows = deps.memory.admin_grievances(status)
        links = insights.grievance_links(store, [r["id"] for r in rows])
        names = {reg["farmer_id"]: reg["name"] for reg in store.list_registered()}
        return [{**r, "farmer_name": names.get(r["farmer_id"]), "payment": links.get(r["id"])} for r in rows]

    @router.patch("/grievances/{interaction_id}", dependencies=[Depends(admin)])
    def update_grievance(interaction_id: int, req: GrievanceUpdate):
        if req.status not in ("new", "in_progress", "resolved"):
            raise HTTPException(400, "status must be one of: new, in_progress, resolved")
        if not deps.memory.admin_update_grievance(interaction_id, req.status, req.note):
            raise HTTPException(404, "Grievance not found")
        grievance = deps.memory.get_grievance(interaction_id)
        if grievance:
            language = store.get_language(grievance["farmer_id"])
            notifications.notify(grievance["farmer_id"], "complaint_update", ref=f"#{interaction_id}",
                                 status=grievance_label(req.status, language), note=req.note or "")
        return {"id": interaction_id, "status": req.status, "note": req.note}

    @router.get("/activity", dependencies=[Depends(admin)])
    def activity(limit: int = 30):
        return insights.activity(store, limit)

    @router.get("/procurement-trends", dependencies=[Depends(admin)])
    def procurement_trends(days: int = 7):
        return insights.trends(store, days)

    @router.get("/mandis", dependencies=[Depends(admin)])
    def mandis():
        return store.list_mandis()

    @router.patch("/mandis/{mandi_id}", dependencies=[Depends(admin)])
    def update_mandi(mandi_id: str, req: MandiUpdate):
        try:
            return store.set_mandi_capacity(mandi_id, req.capacity_per_window)
        except NotFound:
            raise HTTPException(404, "Mandi not found")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @router.post("/announce", dependencies=[Depends(admin)])
    def announce(req: Announcement):
        title, body = " ".join(req.title.split()), " ".join(req.body.split())
        if not title or len(title) > 80:
            raise HTTPException(422, "title must be 1-80 characters")
        if not body or len(body) > 500:
            raise HTTPException(422, "message must be 1-500 characters")
        known = [f["farmer_id"] for f in all_farmers()]
        if req.farmer_id:
            if req.farmer_id not in known:
                raise HTTPException(404, "Unknown farmer")
            recipients = [req.farmer_id]
        else:
            recipients = known
        for farmer_id in recipients:
            notifications.notify(farmer_id, "announcement", title=title, body=body)
        return {"sent": len(recipients)}

    @router.get("/failure-codes", dependencies=[Depends(admin)])
    def failure_codes():
        return payment_kb.code_descriptions()

    return router
