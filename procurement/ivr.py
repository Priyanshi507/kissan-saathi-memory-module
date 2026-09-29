import base64
import hashlib
import hmac
from typing import Dict, Optional
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape, quoteattr

from .complaint import status_label
from .payment_kb import explain

SPOKEN_LANGUAGES = {"hi": "hi-IN", "en": "en-IN", "mr": "mr-IN", "ta": "ta-IN", "te": "te-IN",
                    "bn": "bn-IN", "gu": "gu-IN", "kn": "kn-IN", "pa": "pa-IN", "ml": "ml-IN"}

PROMPTS = {
    "hi": {
        "greeting": "नमस्ते, किसान साथी में आपका स्वागत है। आप भुगतान, कतार या खेती के बारे में पूछ सकते हैं। अपना सवाल बोलिए।",
        "again": "क्या आप कुछ और पूछना चाहते हैं?",
        "silence": "हमें आपकी आवाज़ सुनाई नहीं दी। कृपया दोबारा कोशिश करें।",
        "bye": "धन्यवाद। दोबारा जाँचने के लिए फिर कॉल करें।",
        "error": "क्षमा करें, अभी जवाब नहीं मिल पाया। कृपया थोड़ी देर बाद कोशिश करें।",
    },
    "en": {
        "greeting": "Welcome to Kissan Saathi. You can ask about your payment, your queue, or farming. Please speak your question.",
        "again": "Would you like to ask anything else?",
        "silence": "We could not hear you. Please try again.",
        "bye": "Thank you. Call again to check anytime.",
        "error": "Sorry, we could not get an answer right now. Please try again later.",
    },
}

_PAYMENT_WORDS = ["payment", "paisa", "money", "paid", "credited", "भुगतान", "पैसा", "पैसे", "रुपये", "रुपए"]
_QUEUE_WORDS = ["slot", "queue", "line", "turn", "token", "कतार", "स्लॉट", "नंबर", "बारी", "टोकन"]
MAX_SPOKEN_CHARS = 700


def compute_signature(auth_token: str, url: str, params: Dict[str, str]) -> str:
    payload = url + "".join(key + params[key] for key in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def _url_variants(url: str):
    parsed = urlsplit(url)
    yield url
    default = {"https": 443, "http": 80}.get(parsed.scheme)
    if not default or not parsed.hostname:
        return
    userinfo = parsed.netloc.rpartition("@")[0]
    prefix = f"{userinfo}@" if userinfo else ""
    host = parsed.hostname if ":" not in parsed.hostname else f"[{parsed.hostname}]"
    with_port = urlunsplit((parsed.scheme, f"{prefix}{host}:{parsed.port or default}", parsed.path, parsed.query, parsed.fragment))
    without_port = urlunsplit((parsed.scheme, f"{prefix}{host}", parsed.path, parsed.query, parsed.fragment))
    yield with_port if parsed.port is None else without_port


def valid_signature(auth_token: str, url: str, params: Dict[str, str], signature: Optional[str]) -> bool:
    if not signature:
        return False
    return any(hmac.compare_digest(compute_signature(auth_token, candidate, params), signature)
               for candidate in _url_variants(url))


def farmer_id_from_caller(caller: Optional[str]) -> Optional[str]:
    digits = "".join(ch for ch in (caller or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else None


def short_lang(language: Optional[str]) -> str:
    lang = (language or "hi").split("-")[0].lower()
    return lang if lang in PROMPTS else "en"


def spoken_lang(language: Optional[str], default: str = "hi-IN") -> str:
    if language and "-" in language:
        return language
    return SPOKEN_LANGUAGES.get((language or "").lower(), default)


def _prompt(key: str, language: Optional[str]) -> str:
    return PROMPTS[short_lang(language)][key]


def gather_response(prompt: str, action_url: str, language_code: str, fallback: str) -> str:
    lang = quoteattr(language_code)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f'<Gather input="speech" language={lang} speechTimeout="auto" action={quoteattr(action_url)} method="POST">'
        f"<Say language={lang}>{escape(prompt)}</Say></Gather>"
        f"<Say language={lang}>{escape(fallback)}</Say></Response>"
    )


def say_and_gather(answer: str, follow_up: str, action_url: str, language_code: str, goodbye: str) -> str:
    lang = quoteattr(language_code)
    return (
        '<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"<Say language={lang}>{escape(answer[:MAX_SPOKEN_CHARS])}</Say>"
        f'<Gather input="speech" language={lang} speechTimeout="auto" action={quoteattr(action_url)} method="POST">'
        f"<Say language={lang}>{escape(follow_up)}</Say></Gather>"
        f"<Say language={lang}>{escape(goodbye)}</Say></Response>"
    )


def say_and_hangup(text: str, language_code: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Say language={quoteattr(language_code)}>'
            f"{escape(text[:MAX_SPOKEN_CHARS])}</Say><Hangup/></Response>")


def prompt_for(key: str, language: Optional[str]) -> str:
    return _prompt(key, language)


def answer_from_records(store, farmer_id: str, text: str, language: Optional[str]) -> Optional[str]:
    """Payment and queue questions are answered from the database, never by the model."""
    lowered = (text or "").lower()
    lang = short_lang(language)
    if any(w in lowered for w in _PAYMENT_WORDS):
        payments = store.farmer_payments(farmer_id)
        if not payments:
            return ("अभी आपके नाम कोई भुगतान दर्ज नहीं दिख रहा है।" if lang == "hi"
                    else "I do not see any payment recorded for you yet.")
        p = payments[0]
        amount = f"{p['amount']:,.0f}" if p.get("amount") is not None else None
        status = status_label(p["status"], lang)
        if lang == "hi":
            reply = f"आपके {p.get('crop') or 'उपज'} का भुगतान{' ' + amount + ' रुपये का' if amount else ''} अभी {status} है।"
        else:
            reply = f"Your {p.get('crop') or 'produce'} payment{' of ' + amount + ' rupees' if amount else ''} is {status}."
        if p["status"] == "failed":
            d = explain(p.get("failure_code"), lang)
            reply += f" {d['title']}। {d['steps'][0]}" if lang == "hi" else f" Reason: {d['title']}. {d['steps'][0]}"
        elif p["can_escalate"]:
            reply += (f" इसे {p['days_waiting']} दिन हो गए हैं। आप ऐप में शिकायत दर्ज कर सकते हैं।" if lang == "hi"
                      else f" It has been {p['days_waiting']} days. You can raise a complaint in the app.")
        return reply
    if any(w in lowered for w in _QUEUE_WORDS):
        active = [s for s in store.farmer_slots(farmer_id) if s["queue"]]
        if not active:
            return "आपका कोई आने वाला स्लॉट नहीं दिख रहा है।" if lang == "hi" else "I do not see an upcoming slot for you."
        s = active[0]
        q = s["queue"]
        if lang == "hi":
            return f"{s['mandi_name'] or 'मंडी'} में आप कतार में {q['position']} नंबर पर हैं। अनुमानित इंतज़ार लगभग {q['est_wait_minutes']} मिनट।"
        return f"At {s['mandi_name'] or 'the mandi'} you are number {q['position']} in the line. Estimated wait about {q['est_wait_minutes']} minutes."
    return None
