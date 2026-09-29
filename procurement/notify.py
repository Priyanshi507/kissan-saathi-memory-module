import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

TEMPLATES: Dict[str, Dict[str, Tuple[str, str]]] = {
    "slot_booked": {
        "en": ("Slot confirmed", "Your slot at {mandi} on {date} ({window}) is confirmed. Token {token}."),
        "hi": ("स्लॉट पक्का हुआ", "{mandi} में {date} ({window}) का आपका स्लॉट पक्का हो गया है। टोकन {token}।"),
    },
    "slot_checked_in": {
        "en": ("You are in the queue", "You are number {position} in the queue at {mandi}. Estimated wait about {wait} minutes."),
        "hi": ("आप कतार में हैं", "{mandi} में आप कतार में {position} नंबर पर हैं। अनुमानित इंतज़ार लगभग {wait} मिनट।"),
    },
    "slot_completed": {
        "en": ("Produce received", "Your produce at {mandi} has been received. Your payment is now tracked under Where's my money."),
        "hi": ("उपज ले ली गई", "{mandi} में आपकी उपज ले ली गई है। आपका भुगतान अब 'मेरा पैसा कहाँ है' में दिखेगा।"),
    },
    "slot_rejected": {
        "en": ("Slot could not be confirmed", "Your slot at {mandi} on {date} could not be confirmed because that time is full. Please book another time."),
        "hi": ("स्लॉट पक्का नहीं हो सका", "{mandi} में {date} का आपका स्लॉट पक्का नहीं हो सका क्योंकि वह समय भर चुका है। कृपया दूसरा समय चुनें।"),
    },
    "payment_update": {
        "en": ("Payment update", "Your {crop} payment is now: {status}. {detail}"),
        "hi": ("भुगतान अपडेट", "आपके {crop} के भुगतान की स्थिति: {status}। {detail}"),
    },
    "complaint_registered": {
        "en": ("Complaint registered", "Your complaint has been registered. Reference number {ref}."),
        "hi": ("शिकायत दर्ज हुई", "आपकी शिकायत दर्ज हो गई है। संदर्भ संख्या {ref}।"),
    },
    "complaint_update": {
        "en": ("Complaint update", "Update on complaint {ref}: {status}. {note}"),
        "hi": ("शिकायत अपडेट", "शिकायत {ref} पर अपडेट: {status}। {note}"),
    },
}


class _Safe(dict):
    def __missing__(self, key):
        return ""


def render(kind: str, language: Optional[str], fields: Dict[str, Any]) -> Tuple[str, str]:
    if kind == "announcement":
        return str(fields.get("title", "")).strip(), " ".join(str(fields.get("body", "")).split())
    lang = (language or "en").split("-")[0].lower()
    entry = TEMPLATES.get(kind)
    if not entry:
        raise KeyError(f"Unknown notification kind {kind!r}")
    title, body = entry.get(lang) or entry["en"]
    return title, " ".join(body.format_map(_Safe(fields)).split())


def phone_e164(farmer_id: str, country_code: str = "+91") -> Optional[str]:
    digits = "".join(ch for ch in (farmer_id or "") if ch.isdigit())
    if (farmer_id or "").strip().startswith("+") and len(digits) >= 11:
        return "+" + digits
    return f"{country_code}{digits}" if len(digits) == 10 else None


Transport = Callable[[str, Dict[str, str], bytes, float], Tuple[int, str]]


def _urllib_transport(url: str, headers: Dict[str, str], data: bytes, timeout: float) -> Tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


class LogSms:
    name = "log"

    def send(self, phone: str, body: str) -> Tuple[str, str]:
        return "logged", "SMS gateway not configured; message recorded only"


class TwilioSms:
    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, from_number: Optional[str] = None,
                 messaging_service_sid: Optional[str] = None, transport: Transport = _urllib_transport,
                 timeout: float = 8.0):
        if not (from_number or messaging_service_sid):
            raise ValueError("Set a from number or a messaging service SID")
        self.account_sid, self.auth_token = account_sid, auth_token
        self.from_number, self.messaging_service_sid = from_number, messaging_service_sid
        self.transport, self.timeout = transport, timeout

    def send(self, phone: str, body: str) -> Tuple[str, str]:
        form = {"To": phone, "Body": body}
        if self.messaging_service_sid:
            form["MessagingServiceSid"] = self.messaging_service_sid
        else:
            form["From"] = self.from_number
        auth = base64.b64encode(f"{self.account_sid}:{self.auth_token}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        try:
            status, text = self.transport(url, headers, urllib.parse.urlencode(form).encode(), self.timeout)
        except Exception as exc:
            return "failed", f"{type(exc).__name__}: {exc}"[:200]
        if 200 <= status < 300:
            try:
                return "sent", json.loads(text).get("sid", "")
            except ValueError:
                return "sent", ""
        try:
            detail = json.loads(text).get("message", text)
        except ValueError:
            detail = text
        return "failed", f"HTTP {status}: {detail}"[:200]


def sms_from_env(env: Dict[str, str]):
    sid, token = env.get("TWILIO_ACCOUNT_SID"), env.get("TWILIO_AUTH_TOKEN")
    sender, service = env.get("TWILIO_FROM"), env.get("TWILIO_MESSAGING_SERVICE_SID")
    if sid and token and (sender or service):
        return TwilioSms(sid, token, from_number=sender, messaging_service_sid=service)
    return LogSms()


class NotificationService:
    def __init__(self, store, sms=None, background: bool = True):
        self.store, self.sms, self.background = store, sms or LogSms(), background

    def notify(self, farmer_id: str, kind: str, language: Optional[str] = None, **fields: Any) -> int:
        lang = language or self.store.get_language(farmer_id)
        title, body = render(kind, lang, fields)
        phone = phone_e164(farmer_id)
        notification_id = self.store.add_notification(
            farmer_id, kind, title, body, sms_status="pending" if phone else "no_phone")
        if phone:
            if self.background and isinstance(self.sms, TwilioSms):
                threading.Thread(target=self._deliver, args=(notification_id, phone, body), daemon=True).start()
            else:
                self._deliver(notification_id, phone, body)
        return notification_id

    def _deliver(self, notification_id: int, phone: str, body: str) -> None:
        status, detail = self.sms.send(phone, body)
        self.store.set_sms_result(notification_id, status, detail)
