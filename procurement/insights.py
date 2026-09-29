from datetime import timedelta
from typing import Any, Dict, Iterable, List


def _rows(store, sql: str, params: Iterable = ()) -> List[Dict[str, Any]]:
    with store.lock:
        return [dict(r) for r in store.conn.execute(sql, tuple(params)).fetchall()]


def activity(store, limit: int = 30) -> List[Dict[str, Any]]:
    """One timeline of everything farmers and staff did, newest first. Text is left to the client to translate."""
    limit = max(1, min(int(limit), 200))
    names = {r["farmer_id"]: r["name"] for r in _rows(store, "SELECT farmer_id, name FROM farmer_registry")}
    items: List[Dict[str, Any]] = []

    def add(ts, kind, farmer_id, **data):
        if ts:
            items.append({"ts": ts, "kind": kind, "farmer_id": farmer_id, "farmer_name": names.get(farmer_id), "data": data})

    for r in _rows(store, "SELECT farmer_id, primary_crops, registered_at FROM farmer_registry ORDER BY registered_at DESC LIMIT ?", (limit,)):
        add(r["registered_at"], "registered", r["farmer_id"], crops=r["primary_crops"])

    for r in _rows(store, "SELECT farmer_id, ts, crop, query_text, is_grievance FROM interactions ORDER BY ts DESC LIMIT ?", (limit,)):
        kind = "complaint" if r["is_grievance"] else "chat"
        add(r["ts"], kind, r["farmer_id"], crop=r["crop"], text=(r["query_text"] or "").strip().replace("\n", " ")[:140])

    slot_sql = (
        "SELECT s.farmer_id, s.token, s.crop, s.slot_date, s.window, s.created_at, s.checked_in_at, s.completed_at, "
        "COALESCE(m.name, s.mandi_id) AS mandi FROM slots s LEFT JOIN mandis m ON m.id = s.mandi_id ORDER BY s.id DESC LIMIT ?"
    )
    for r in _rows(store, slot_sql, (limit,)):
        common = {"token": r["token"], "mandi": r["mandi"], "crop": r["crop"], "date": r["slot_date"], "window": r["window"]}
        add(r["created_at"], "slot_booked", r["farmer_id"], **common)
        add(r["checked_in_at"], "slot_checked_in", r["farmer_id"], **common)
        add(r["completed_at"], "slot_completed", r["farmer_id"], **common)

    payment_sql = (
        "SELECT e.ts, e.status, e.failure_code, e.note, p.farmer_id, p.crop, p.amount FROM payment_events e "
        "JOIN payments p ON p.id = e.payment_id ORDER BY e.id DESC LIMIT ?"
    )
    for r in _rows(store, payment_sql, (limit,)):
        add(r["ts"], "payment", r["farmer_id"], status=r["status"], reason=r["failure_code"], crop=r["crop"], amount=r["amount"], note=r["note"])

    items.sort(key=lambda item: item["ts"], reverse=True)
    return items[:limit]


def trends(store, days: int = 7) -> Dict[str, Any]:
    """Bookings and completions per day, and where payments stand."""
    days = max(1, min(int(days), 60))
    today = store._today()
    per_day = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        booked = _rows(store, "SELECT COUNT(*) AS c FROM slots WHERE substr(created_at, 1, 10) = ?", (day,))[0]["c"]
        completed = _rows(store, "SELECT COUNT(*) AS c FROM slots WHERE substr(completed_at, 1, 10) = ?", (day,))[0]["c"]
        per_day.append({"date": day, "booked": booked, "completed": completed})

    payments = {status: {"count": 0, "amount": 0.0} for status in ("pending", "processing", "paid", "failed")}
    for r in _rows(store, "SELECT status, COUNT(*) AS c, COALESCE(SUM(amount), 0) AS a FROM payments GROUP BY status"):
        payments[r["status"]] = {"count": r["c"], "amount": float(r["a"])}
    return {"days": per_day, "payments": payments}


def farmer_counts(store) -> Dict[str, Dict[str, int]]:
    counts: Dict[str, Dict[str, int]] = {}
    for r in _rows(store, "SELECT farmer_id, COUNT(*) AS c FROM slots WHERE status != 'cancelled' GROUP BY farmer_id"):
        counts.setdefault(r["farmer_id"], {"slots": 0, "payments": 0, "open_payments": 0})["slots"] = r["c"]
    for r in _rows(store, "SELECT farmer_id, COUNT(*) AS c, SUM(CASE WHEN status != 'paid' THEN 1 ELSE 0 END) AS o FROM payments GROUP BY farmer_id"):
        entry = counts.setdefault(r["farmer_id"], {"slots": 0, "payments": 0, "open_payments": 0})
        entry["payments"], entry["open_payments"] = r["c"], r["o"] or 0
    return counts


def grievance_links(store, grievance_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Which payment, if any, each complaint was raised about."""
    if not grievance_ids:
        return {}
    marks = ",".join("?" for _ in grievance_ids)
    rows = _rows(store, f"SELECT id, grievance_id, crop, amount, status, sold_on FROM payments WHERE grievance_id IN ({marks})", grievance_ids)
    return {r["grievance_id"]: {"payment_id": r["id"], "crop": r["crop"], "amount": r["amount"], "status": r["status"], "sold_on": r["sold_on"]} for r in rows}
