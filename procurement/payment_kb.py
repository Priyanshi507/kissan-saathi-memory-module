import json
import re
from typing import Any, Dict, List, Optional

CODES = (
    "aadhaar_not_seeded", "name_mismatch", "wrong_account_or_ifsc", "dormant_or_closed_account",
    "other_bank_mapped", "record_verification_pending", "processing_delay", "unknown",
)

# Only channels confirmed to exist. Re-verify before any real deployment.
OFFICIAL_CHANNELS = [
    {"name": "PFMS - Know Your Payment", "site": "pfms.nic.in"},
    {"name": "CPGRAMS - public grievance portal", "site": "pgportal.gov.in"},
]

KB: Dict[str, Dict[str, Dict[str, Any]]] = {
    "aadhaar_not_seeded": {
        "en": {
            "title": "Aadhaar is not linked for payments",
            "cause": "Government payments go to the bank account your Aadhaar is linked to for benefit payments. If your bank has not linked (seeded) your Aadhaar for this, the money cannot reach you.",
            "steps": [
                "Go to your bank branch and ask them to link your Aadhaar to your account for government benefit payments (DBT).",
                "Ask for a receipt or request number.",
                "When the bank confirms, tell the procurement centre so they can retry the payment.",
            ],
            "carry": ["Aadhaar card", "Bank passbook", "Mobile number linked to the account"],
        },
        "hi": {
            "title": "आधार भुगतान के लिए जुड़ा नहीं है",
            "cause": "सरकारी भुगतान उस बैंक खाते में जाता है जिससे आपका आधार लाभ भुगतान के लिए जुड़ा हो। अगर बैंक ने आपका आधार इसके लिए नहीं जोड़ा है, तो पैसा आप तक नहीं पहुँच सकता।",
            "steps": [
                "अपनी बैंक शाखा जाकर कहें कि आपका आधार सरकारी लाभ भुगतान (DBT) के लिए आपके खाते से जोड़ दें।",
                "रसीद या अनुरोध संख्या ज़रूर लें।",
                "बैंक के पुष्टि करने पर खरीद केंद्र को बताएँ ताकि वे भुगतान दोबारा भेज सकें।",
            ],
            "carry": ["आधार कार्ड", "बैंक पासबुक", "खाते से जुड़ा मोबाइल नंबर"],
        },
    },
    "name_mismatch": {
        "en": {
            "title": "Name does not match exactly",
            "cause": "The name on your Aadhaar and the name on your bank account are different, even slightly (spelling, initials or the order of names). Payments can be rejected for this.",
            "steps": [
                "Compare the name on your Aadhaar with the name on your bank passbook.",
                "If they differ, get one corrected. The bank can correct its record; Aadhaar corrections are done at an Aadhaar update centre.",
                "Tell the procurement centre once it is corrected so the payment is retried.",
            ],
            "carry": ["Aadhaar card", "Bank passbook", "Another ID proof"],
        },
        "hi": {
            "title": "नाम बिल्कुल मेल नहीं खाता",
            "cause": "आपके आधार और बैंक खाते में नाम थोड़ा भी अलग है (स्पेलिंग, इनिशियल या नामों का क्रम)। इस वजह से भुगतान रुक सकता है।",
            "steps": [
                "अपने आधार का नाम और बैंक पासबुक का नाम मिलाकर देखें।",
                "अगर अलग हैं तो किसी एक को ठीक करवाएँ। बैंक अपना रिकॉर्ड ठीक कर सकता है; आधार में सुधार आधार अपडेट केंद्र पर होता है।",
                "सुधार के बाद खरीद केंद्र को बताएँ ताकि भुगतान दोबारा भेजा जाए।",
            ],
            "carry": ["आधार कार्ड", "बैंक पासबुक", "कोई और पहचान पत्र"],
        },
    },
    "wrong_account_or_ifsc": {
        "en": {
            "title": "Wrong account number or IFSC on record",
            "cause": "The bank account number or IFSC code recorded at the centre is wrong, or you have changed your bank account since registering.",
            "steps": [
                "Check the account number and IFSC printed on your passbook.",
                "Give the correct details in writing to the procurement centre in-charge so the record is corrected.",
                "Ask them to retry the payment after the correction.",
            ],
            "carry": ["Passbook front page", "Cancelled cheque if you have one"],
        },
        "hi": {
            "title": "रिकॉर्ड में गलत खाता संख्या या IFSC",
            "cause": "खरीद केंद्र में दर्ज बैंक खाता संख्या या IFSC कोड गलत है, या पंजीकरण के बाद आपने बैंक खाता बदल लिया है।",
            "steps": [
                "अपनी पासबुक पर छपी खाता संख्या और IFSC जाँचें।",
                "सही जानकारी लिखित में खरीद केंद्र प्रभारी को दें ताकि रिकॉर्ड ठीक हो।",
                "सुधार के बाद भुगतान दोबारा भेजने को कहें।",
            ],
            "carry": ["पासबुक का पहला पन्ना", "रद्द किया गया चेक (अगर हो)"],
        },
    },
    "dormant_or_closed_account": {
        "en": {
            "title": "Bank account is inactive or closed",
            "cause": "The account is dormant, frozen or closed, so it cannot receive money.",
            "steps": [
                "Visit your bank and ask them to reactivate the account. They may ask for KYC papers.",
                "Or give the centre the details of another active account in your own name.",
            ],
            "carry": ["Aadhaar card", "Passbook", "KYC documents the bank asks for"],
        },
        "hi": {
            "title": "बैंक खाता निष्क्रिय या बंद है",
            "cause": "खाता निष्क्रिय, फ्रीज़ या बंद है, इसलिए उसमें पैसा नहीं आ सकता।",
            "steps": [
                "अपने बैंक जाकर खाता दोबारा चालू करने को कहें। वे KYC के कागज़ माँग सकते हैं।",
                "या अपने ही नाम के किसी दूसरे चालू खाते की जानकारी केंद्र को दें।",
            ],
            "carry": ["आधार कार्ड", "पासबुक", "बैंक द्वारा माँगे गए KYC दस्तावेज़"],
        },
    },
    "other_bank_mapped": {
        "en": {
            "title": "Aadhaar may be linked to a different, older account",
            "cause": "Only one bank account can be linked to your Aadhaar for benefit payments at a time. If you linked a newer account earlier or later, the money may have gone to a different account than the one you are checking.",
            "steps": [
                "Ask your bank which account your Aadhaar is currently linked to for benefit payments.",
                "Check the passbook of any older account you have, in case the money went there.",
                "If you want payments in a different account, ask that bank to link your Aadhaar to it.",
            ],
            "carry": ["Aadhaar card", "Passbooks of all your accounts"],
        },
        "hi": {
            "title": "आधार शायद किसी दूसरे, पुराने खाते से जुड़ा है",
            "cause": "लाभ भुगतान के लिए एक समय में आपके आधार से केवल एक बैंक खाता जुड़ा हो सकता है। अगर आपने कोई और खाता जोड़ा था, तो पैसा उस खाते में गया हो सकता है जिसे आप नहीं देख रहे।",
            "steps": [
                "बैंक से पूछें कि लाभ भुगतान के लिए आपका आधार अभी किस खाते से जुड़ा है।",
                "अपने किसी पुराने खाते की पासबुक भी जाँचें, हो सकता है पैसा वहाँ गया हो।",
                "अगर आप दूसरे खाते में पैसा चाहते हैं, तो उस बैंक से आधार वहाँ जोड़ने को कहें।",
            ],
            "carry": ["आधार कार्ड", "आपके सभी खातों की पासबुक"],
        },
    },
    "record_verification_pending": {
        "en": {
            "title": "Documents or gate pass still being verified",
            "cause": "Your gate pass, forms or land and crop records are still being verified at the centre, so the payment has not been released.",
            "steps": [
                "Ask the centre in-charge exactly which document or check is pending.",
                "Carry your gate pass or token receipt and the papers you gave at registration.",
                "Ask for a written note of what is pending and by when it will be done.",
            ],
            "carry": ["Gate pass / token receipt", "Weighment slip", "Registration papers"],
        },
        "hi": {
            "title": "दस्तावेज़ या गेट पास का सत्यापन बाकी है",
            "cause": "आपके गेट पास, फ़ॉर्म या ज़मीन-फसल के रिकॉर्ड का सत्यापन केंद्र पर अभी बाकी है, इसलिए भुगतान जारी नहीं हुआ।",
            "steps": [
                "केंद्र प्रभारी से पूछें कि ठीक कौन सा दस्तावेज़ या जाँच बाकी है।",
                "अपना गेट पास या टोकन रसीद और पंजीकरण के कागज़ साथ ले जाएँ।",
                "क्या बाकी है और कब तक होगा, यह लिखित में माँगें।",
            ],
            "carry": ["गेट पास / टोकन रसीद", "तौल पर्ची", "पंजीकरण के कागज़"],
        },
    },
    "processing_delay": {
        "en": {
            "title": "Payment is still being processed",
            "cause": "Your payment has been sent for processing and has not been credited yet. This can take some days.",
            "steps": [
                "Keep your gate pass and weighment slip safe.",
                "Check your passbook or bank SMS after a few working days.",
                "If it is still not credited after the waiting period, use Raise a complaint in this app.",
            ],
            "carry": ["Gate pass", "Weighment slip"],
        },
        "hi": {
            "title": "भुगतान अभी प्रक्रिया में है",
            "cause": "आपका भुगतान प्रक्रिया के लिए भेजा गया है और अभी खाते में नहीं आया। इसमें कुछ दिन लग सकते हैं।",
            "steps": [
                "अपना गेट पास और तौल पर्ची संभालकर रखें।",
                "कुछ कार्य-दिवस बाद पासबुक या बैंक SMS देखें।",
                "इंतज़ार की अवधि के बाद भी पैसा न आए तो इस ऐप में शिकायत दर्ज करें।",
            ],
            "carry": ["गेट पास", "तौल पर्ची"],
        },
    },
    "unknown": {
        "en": {
            "title": "We could not tell the exact reason",
            "cause": "We could not tell the exact reason from what you shared.",
            "steps": [
                "Ask the procurement centre in-charge for the exact reason recorded against your payment.",
                "Type it here or upload a photo of the message and we will explain what it means.",
            ],
            "carry": ["Gate pass", "Weighment slip"],
        },
        "hi": {
            "title": "सही कारण हम नहीं पहचान सके",
            "cause": "आपने जो बताया उससे हम सही कारण नहीं पहचान सके।",
            "steps": [
                "खरीद केंद्र प्रभारी से पूछें कि आपके भुगतान के आगे कारण क्या दर्ज है।",
                "उसे यहाँ लिखें या संदेश की फ़ोटो डालें, हम उसका मतलब समझाएँगे।",
            ],
            "carry": ["गेट पास", "तौल पर्ची"],
        },
    },
}

# Order matters: specific causes are checked before the broad "aadhaar" keyword.
_KEYWORDS = [
    ("name_mismatch", ["name mismatch", "name does not match", "name not match", "name mis", "नाम मेल", "नाम अलग", "नाम गलत"]),
    ("other_bank_mapped", ["another bank", "different bank", "other bank", "old account", "mapped to", "दूसरे बैंक", "पुराने खाते", "पुराना खाता"]),
    ("wrong_account_or_ifsc", ["ifsc", "invalid account", "wrong account", "account number", "account not found", "आईएफएससी", "खाता संख्या", "गलत खाता"]),
    ("dormant_or_closed_account", ["dormant", "inactive", "closed", "frozen", "blocked", "निष्क्रिय", "बंद खाता", "खाता बंद"]),
    ("aadhaar_not_seeded", ["aadhaar", "aadhar", "npci", "seed", "not linked", "आधार", "सीडिंग", "लिंक नहीं"]),
    ("record_verification_pending", ["verification", "gate pass", "gatepass", "document", "form", "सत्यापन", "गेट पास", "दस्तावेज"]),
    ("processing_delay", ["processing", "in process", "under process", "प्रक्रिया"]),
]


def classify_text(text: Optional[str]) -> Optional[str]:
    lowered = (text or "").lower()
    if not lowered.strip():
        return None
    for code, words in _KEYWORDS:
        if any(w in lowered for w in words):
            return code
    return None


def explain(code: Optional[str], language: Optional[str] = None) -> Dict[str, Any]:
    key = code if code in KB else "unknown"
    lang = (language or "en").split("-")[0].lower()
    entry = KB[key].get(lang) or KB[key]["en"]
    return {"code": key, "language": lang if lang in KB[key] else "en", **entry, "official_channels": OFFICIAL_CHANNELS}


def code_descriptions() -> List[Dict[str, str]]:
    return [{"code": c, "meaning": KB[c]["en"]["title"]} for c in CODES if c != "unknown"]


def classifier_prompt() -> str:
    options = "\n".join(f"- {d['code']}: {d['meaning']}" for d in code_descriptions())
    return (
        "You help a farmer understand why a government crop payment failed or is delayed. "
        "Read the farmer's text and/or the message or screen shown in the image, and choose the single best matching cause.\n"
        f"Allowed codes:\n{options}\n- unknown: the text or image does not say why\n"
        "Never invent a cause that is not stated or clearly implied. "
        'Reply with ONLY a JSON object like {"code": "name_mismatch"}.'
    )


def parse_code(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        code = json.loads(text[start:end + 1]).get("code")
    except (ValueError, AttributeError):
        return None
    return code if code in CODES and code != "unknown" else None
