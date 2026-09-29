from typing import Any, Dict, List, Optional


def _location(reg: Optional[Dict[str, Any]]) -> Optional[str]:
    if not reg:
        return None
    parts = [p for p in (reg.get("district"), reg.get("state")) if p]
    return ", ".join(parts) if parts else None


def merge_farmers(chat_rows: List[Dict[str, Any]], registered: List[Dict[str, Any]], search: Optional[str] = None,
                  languages: Optional[Dict[str, str]] = None, counts: Optional[Dict[str, Dict[str, int]]] = None
                  ) -> List[Dict[str, Any]]:
    """One row per farmer: everyone who has chatted plus everyone who has registered in the app."""
    languages, counts = languages or {}, counts or {}
    rows: Dict[str, Dict[str, Any]] = {}
    for chat in chat_rows:
        rows[chat["farmer_id"]] = {**chat, "name": None, "registered": False, "location": None}

    for reg in registered:
        row = rows.get(reg["farmer_id"])
        if row is None:
            row = rows[reg["farmer_id"]] = {
                "farmer_id": reg["farmer_id"], "message_count": 0, "grievance_count": 0,
                "first_seen": reg["registered_at"], "last_active": reg["updated_at"], "crops": [],
            }
        row["name"] = reg["name"]
        row["registered"] = True
        row["location"] = _location(reg)
        if not row["crops"]:
            row["crops"] = list(reg["primary_crops"])

    for farmer_id, row in rows.items():
        row["language"] = languages.get(farmer_id)
        activity = counts.get(farmer_id, {})
        row["slot_count"] = activity.get("slots", 0)
        row["payment_count"] = activity.get("payments", 0)
        row["open_payments"] = activity.get("open_payments", 0)

    result = list(rows.values())
    needle = (search or "").strip().lower()
    if needle:
        result = [r for r in result if needle in r["farmer_id"].lower() or needle in (r["name"] or "").lower()
                  or needle in (r["location"] or "").lower()]
    return sorted(result, key=lambda r: r["last_active"] or "", reverse=True)
