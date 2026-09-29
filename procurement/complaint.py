from datetime import date
from typing import Any, Dict, Optional

from .payment_kb import explain

STATUS_LABELS = {
    "en": {"pending": "Pending", "processing": "Processing", "paid": "Paid", "failed": "Failed"},
    "hi": {"pending": "लंबित", "processing": "प्रक्रिया में", "paid": "भुगतान हो गया", "failed": "विफल"},
}


GRIEVANCE_LABELS = {
    "en": {"new": "Received", "in_progress": "In progress", "resolved": "Resolved"},
    "hi": {"new": "प्राप्त हुई", "in_progress": "कार्रवाई जारी है", "resolved": "हल हो गई"},
}


def grievance_label(status: str, language: Optional[str]) -> str:
    lang = (language or "en").split("-")[0].lower()
    return GRIEVANCE_LABELS.get(lang, GRIEVANCE_LABELS["en"]).get(status, status)


def status_label(status: str, language: Optional[str]) -> str:
    lang = (language or "en").split("-")[0].lower()
    return STATUS_LABELS.get(lang, STATUS_LABELS["en"]).get(status, status)


def _money(value: Optional[float]) -> str:
    return f"Rs {value:,.0f}" if value is not None else "-"


def build_complaint(payment: Dict[str, Any], farmer_id: str, language: Optional[str] = None) -> str:
    """Facts only: every line comes from a stored record, nothing is generated."""
    lang = (language or "en").split("-")[0].lower()
    lang = lang if lang in STATUS_LABELS else "en"
    reason = explain(payment.get("failure_code"), lang)["title"] if payment.get("failure_code") else "-"
    qty = payment.get("quantity_qtl")
    crop_line = f"{payment.get('crop') or '-'}, {qty:g} " if qty is not None else f"{payment.get('crop') or '-'}, - "
    timeline = "\n".join(
        f"- {e['ts'][:10]}: {status_label(e['status'], lang)}" + (f" ({e['note']})" if e.get("note") else "")
        for e in payment.get("timeline", [])
    )

    if lang == "hi":
        return (
            "विषय: MSP पर बेची गई उपज का भुगतान नहीं मिला\n\n"
            f"किसान का मोबाइल नंबर: {farmer_id}\n"
            f"फसल और मात्रा: {crop_line}क्विंटल\n"
            f"खरीद केंद्र: {payment.get('mandi_name') or '-'}\n"
            f"बिक्री की तारीख: {payment['sold_on']}\n"
            f"अपेक्षित राशि: {_money(payment.get('amount'))}\n"
            f"आज भुगतान की स्थिति: {status_label(payment['status'], lang)} (बिक्री के {payment['days_waiting']} दिन बाद)\n"
            f"दर्ज कारण: {reason}\n\n"
            f"घटनाक्रम:\n{timeline}\n\n"
            "निवेदन: कृपया मेरे भुगतान की जाँच करें और देरी का कारण तथा खाते में आने की तारीख बताएँ।"
        )
    return (
        "Subject: Payment not received for produce sold at MSP\n\n"
        f"Farmer mobile number: {farmer_id}\n"
        f"Crop and quantity: {crop_line}quintal\n"
        f"Procurement centre: {payment.get('mandi_name') or '-'}\n"
        f"Date of sale: {payment['sold_on']}\n"
        f"Amount expected: {_money(payment.get('amount'))}\n"
        f"Payment status today: {status_label(payment['status'], lang)} ({payment['days_waiting']} days since sale)\n"
        f"Reason recorded: {reason}\n\n"
        f"Timeline:\n{timeline}\n\n"
        "Request: Please check my payment and tell me the reason for the delay and the date by which it will be credited."
    )
