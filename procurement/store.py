import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

SLOT_ACTIVE = ("booked", "checked_in")
SLOT_STATUSES = ("booked", "checked_in", "completed", "no_show", "cancelled")
PAYMENT_STATUSES = ("pending", "processing", "paid", "failed")

DEFAULT_CAPACITY = 20
DEFAULT_SERVICE_MINUTES = 6
SERVICE_SAMPLE_SIZE = 30
MIN_SERVICE_MINUTES = 1.0
ESCALATE_AFTER_DAYS = 7

_SLOT_TRANSITIONS = {
    "booked": ("checked_in", "no_show", "cancelled"),
    "checked_in": ("completed", "cancelled"),
}

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


class WindowFull(Exception):
    pass


class NotFound(Exception):
    pass


class InvalidTransition(Exception):
    pass


def parse_date(text: str) -> str:
    """Accepts ISO ('2026-09-16') or the app's '16 Sep 2026'; returns ISO."""
    value = (text or "").strip()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    parts = value.split()
    if len(parts) == 3 and parts[1][:3].lower() in _MONTHS:
        try:
            return date(int(parts[2]), _MONTHS[parts[1][:3].lower()], int(parts[0])).isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unrecognised date: {text!r}")


class ProcurementStore:
    def __init__(self, storage, now: Callable[[], datetime] = datetime.utcnow):
        self.conn = storage.conn
        self.lock = storage.lock
        self.now = now
        self._init_schema()

    def _init_schema(self):
        with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mandis (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, district TEXT, state TEXT,
                    latitude REAL, longitude REAL,
                    capacity_per_window INTEGER NOT NULL DEFAULT 20
                );
                CREATE TABLE IF NOT EXISTS slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id TEXT UNIQUE,
                    farmer_id TEXT NOT NULL, token TEXT NOT NULL, mandi_id TEXT NOT NULL,
                    crop TEXT, quantity_qtl REAL,
                    slot_date TEXT NOT NULL, window TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'booked',
                    created_at TEXT NOT NULL, checked_in_at TEXT, completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_slots_farmer ON slots (farmer_id, slot_date);
                CREATE INDEX IF NOT EXISTS idx_slots_window ON slots (mandi_id, slot_date, window, status);
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    farmer_id TEXT NOT NULL, slot_id INTEGER, crop TEXT,
                    quantity_qtl REAL, amount REAL, sold_on TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    failure_code TEXT, note TEXT, grievance_id INTEGER,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_payments_farmer ON payments (farmer_id, sold_on);
                CREATE TABLE IF NOT EXISTS payment_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id INTEGER NOT NULL,
                    ts TEXT NOT NULL, status TEXT NOT NULL, failure_code TEXT, note TEXT
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, farmer_id TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                    sms_status TEXT NOT NULL DEFAULT 'none', sms_detail TEXT,
                    created_at TEXT NOT NULL, read_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_notifications_farmer ON notifications (farmer_id, id);
                CREATE TABLE IF NOT EXISTS farmer_prefs (
                    farmer_id TEXT PRIMARY KEY, language TEXT
                );
                CREATE TABLE IF NOT EXISTS farmer_registry (
                    farmer_id TEXT PRIMARY KEY, name TEXT, primary_crops TEXT,
                    registered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    district TEXT, state TEXT
                );
                """
            )
            existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(farmer_registry)").fetchall()}
            for column in ("district", "state"):
                if column not in existing:
                    self.conn.execute(f"ALTER TABLE farmer_registry ADD COLUMN {column} TEXT")
            self.conn.commit()

    def _iso_now(self) -> str:
        return self.now().isoformat()

    def _today(self) -> date:
        return self.now().date()

    # ---------- mandis ----------

    def upsert_mandi(self, mandi_id: str, name: str, latitude: Optional[float] = None,
                     longitude: Optional[float] = None, district: Optional[str] = None,
                     state: Optional[str] = None) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO mandis (id, name, district, state, latitude, longitude)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    district = COALESCE(excluded.district, district),
                    state = COALESCE(excluded.state, state),
                    latitude = COALESCE(excluded.latitude, latitude),
                    longitude = COALESCE(excluded.longitude, longitude)
                """,
                (mandi_id, name, district, state, latitude, longitude),
            )
            self.conn.commit()

    def list_mandis(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, name, district, state, latitude, longitude, capacity_per_window FROM mandis ORDER BY name"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_mandi_capacity(self, mandi_id: str, capacity: int) -> Dict[str, Any]:
        if not isinstance(capacity, int) or isinstance(capacity, bool) or not 1 <= capacity <= 500:
            raise ValueError("capacity must be a whole number from 1 to 500")
        with self.lock:
            cur = self.conn.execute("UPDATE mandis SET capacity_per_window = ? WHERE id = ?", (capacity, mandi_id))
            if cur.rowcount == 0:
                raise NotFound("Mandi not found")
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id, name, district, state, latitude, longitude, capacity_per_window FROM mandis WHERE id = ?", (mandi_id,)
            ).fetchone()
        return dict(row)

    # ---------- slots and queue ----------

    def book_slot(self, client_id: str, farmer_id: str, token: str, mandi_id: str,
                  crop: Optional[str], quantity_qtl: Optional[float],
                  slot_date: str, window: str) -> Dict[str, Any]:
        """Idempotent on client_id, so an offline booking replayed twice is one slot."""
        iso_date = parse_date(slot_date)
        with self.lock:
            existing = self.conn.execute("SELECT id FROM slots WHERE client_id = ?", (client_id,)).fetchone()
            if existing:
                return {**self._slot_view(existing["id"]), "created": False}

            mandi = self.conn.execute("SELECT capacity_per_window FROM mandis WHERE id = ?", (mandi_id,)).fetchone()
            capacity = mandi["capacity_per_window"] if mandi else DEFAULT_CAPACITY
            taken = self.conn.execute(
                "SELECT COUNT(*) AS c FROM slots WHERE mandi_id = ? AND slot_date = ? AND window = ? AND status IN ('booked','checked_in')",
                (mandi_id, iso_date, window),
            ).fetchone()["c"]
            if taken >= capacity:
                raise WindowFull(f"{window} on {iso_date} is full ({taken}/{capacity})")

            cur = self.conn.execute(
                "INSERT INTO slots (client_id, farmer_id, token, mandi_id, crop, quantity_qtl, slot_date, window, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client_id, farmer_id, token, mandi_id, crop, quantity_qtl, iso_date, window, self._iso_now()),
            )
            self.conn.commit()
            return {**self._slot_view(cur.lastrowid), "created": True}

    def cancel_slot(self, client_id: str, farmer_id: str) -> Dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT id, status FROM slots WHERE client_id = ? AND farmer_id = ?",
                                    (client_id, farmer_id)).fetchone()
            if not row:
                raise NotFound("Slot not found")
            if row["status"] in SLOT_ACTIVE:
                self.conn.execute("UPDATE slots SET status = 'cancelled' WHERE id = ?", (row["id"],))
                self.conn.commit()
            return self._slot_view(row["id"])

    def set_slot_status(self, slot_id: int, status: str) -> Dict[str, Any]:
        if status not in SLOT_STATUSES:
            raise InvalidTransition(f"Unknown status {status!r}")
        with self.lock:
            row = self.conn.execute("SELECT status FROM slots WHERE id = ?", (slot_id,)).fetchone()
            if not row:
                raise NotFound("Slot not found")
            if status not in _SLOT_TRANSITIONS.get(row["status"], ()):
                raise InvalidTransition(f"Cannot move a slot from {row['status']} to {status}")
            stamp = {"checked_in": "checked_in_at", "completed": "completed_at"}.get(status)
            if stamp:
                self.conn.execute(f"UPDATE slots SET status = ?, {stamp} = ? WHERE id = ?", (status, self._iso_now(), slot_id))
            else:
                self.conn.execute("UPDATE slots SET status = ? WHERE id = ?", (status, slot_id))
            self.conn.commit()
            return self._slot_view(slot_id)

    def _active_order(self, mandi_id: str, slot_date: str, window: str) -> List[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT id, status, created_at, checked_in_at FROM slots "
            "WHERE mandi_id = ? AND slot_date = ? AND window = ? AND status IN ('booked','checked_in')",
            (mandi_id, slot_date, window),
        ).fetchall()
        # Farmers who have arrived are served first, in arrival order; the rest by booking time.
        return sorted(rows, key=lambda r: (0 if r["status"] == "checked_in" else 1,
                                           r["checked_in_at"] or r["created_at"], r["id"]))

    def _avg_service_minutes(self, mandi_id: str) -> Dict[str, Any]:
        rows = self.conn.execute(
            "SELECT checked_in_at, completed_at FROM slots WHERE mandi_id = ? AND status = 'completed' "
            "AND checked_in_at IS NOT NULL AND completed_at IS NOT NULL ORDER BY completed_at DESC LIMIT ?",
            (mandi_id, SERVICE_SAMPLE_SIZE),
        ).fetchall()
        minutes = []
        for r in rows:
            delta = (datetime.fromisoformat(r["completed_at"]) - datetime.fromisoformat(r["checked_in_at"])).total_seconds() / 60
            if delta >= MIN_SERVICE_MINUTES:
                minutes.append(delta)
        if minutes:
            return {"minutes": sum(minutes) / len(minutes), "measured": True, "samples": len(minutes)}
        return {"minutes": float(DEFAULT_SERVICE_MINUTES), "measured": False, "samples": 0}

    def _slot_view(self, slot_id: int) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT s.*, m.name AS mandi_name, m.latitude, m.longitude, f.name AS farmer_name "
            "FROM slots s LEFT JOIN mandis m ON m.id = s.mandi_id "
            "LEFT JOIN farmer_registry f ON f.farmer_id = s.farmer_id WHERE s.id = ?", (slot_id,)
        ).fetchone()
        if not row:
            raise NotFound("Slot not found")
        view = dict(row)
        view["queue"] = None
        if row["status"] in SLOT_ACTIVE:
            order = self._active_order(row["mandi_id"], row["slot_date"], row["window"])
            ids = [r["id"] for r in order]
            ahead = ids.index(slot_id)
            service = self._avg_service_minutes(row["mandi_id"])
            view["queue"] = {
                "position": ahead + 1,
                "ahead": ahead,
                "est_wait_minutes": round(ahead * service["minutes"]),
                "estimate_is_measured": service["measured"],
                "service_samples": service["samples"],
            }
        return view

    def farmer_slots(self, farmer_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM slots WHERE farmer_id = ? ORDER BY slot_date DESC, id DESC LIMIT ?", (farmer_id, limit)
            ).fetchall()]
            return [self._slot_view(i) for i in ids]

    def admin_slots(self, status: Optional[str] = None, slot_date: Optional[str] = None,
                    mandi_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        query, params = "SELECT id FROM slots WHERE 1=1", []
        if status:
            query += " AND status = ?"; params.append(status)
        if slot_date:
            query += " AND slot_date = ?"; params.append(parse_date(slot_date))
        if mandi_id:
            query += " AND mandi_id = ?"; params.append(mandi_id)
        query += " ORDER BY slot_date DESC, id DESC LIMIT ?"; params.append(limit)
        with self.lock:
            return [self._slot_view(r["id"]) for r in self.conn.execute(query, params).fetchall()]

    def window_load(self, slot_date: Optional[str] = None) -> List[Dict[str, Any]]:
        iso = parse_date(slot_date) if slot_date else self._today().isoformat()
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT s.mandi_id, COALESCE(m.name, s.mandi_id) AS mandi_name, s.window,
                       COALESCE(m.capacity_per_window, ?) AS capacity,
                       SUM(CASE WHEN s.status IN ('booked','checked_in') THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN s.status = 'checked_in' THEN 1 ELSE 0 END) AS arrived,
                       SUM(CASE WHEN s.status = 'completed' THEN 1 ELSE 0 END) AS completed
                FROM slots s LEFT JOIN mandis m ON m.id = s.mandi_id
                WHERE s.slot_date = ? GROUP BY s.mandi_id, s.window ORDER BY mandi_name, s.window
                """,
                (DEFAULT_CAPACITY, iso),
            ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                service = self._avg_service_minutes(r["mandi_id"])
                item["avg_service_minutes"] = round(service["minutes"], 1)
                item["service_measured"] = service["measured"]
                item["date"] = iso
                result.append(item)
            return result

    # ---------- payments ----------

    def create_payment(self, farmer_id: str, crop: Optional[str], quantity_qtl: Optional[float],
                       amount: Optional[float], sold_on: Optional[str] = None,
                       slot_id: Optional[int] = None, note: Optional[str] = None) -> Dict[str, Any]:
        sold = parse_date(sold_on) if sold_on else self._today().isoformat()
        now = self._iso_now()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO payments (farmer_id, slot_id, crop, quantity_qtl, amount, sold_on, status, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (farmer_id, slot_id, crop, quantity_qtl, amount, sold, note, now, now),
            )
            self.conn.execute("INSERT INTO payment_events (payment_id, ts, status, note) VALUES (?, ?, 'pending', ?)",
                              (cur.lastrowid, now, note))
            self.conn.commit()
            return self._payment_view(cur.lastrowid)

    def update_payment(self, payment_id: int, status: str, failure_code: Optional[str] = None,
                       note: Optional[str] = None) -> Dict[str, Any]:
        if status not in PAYMENT_STATUSES:
            raise InvalidTransition(f"Unknown payment status {status!r}")
        now = self._iso_now()
        with self.lock:
            if not self.conn.execute("SELECT 1 FROM payments WHERE id = ?", (payment_id,)).fetchone():
                raise NotFound("Payment not found")
            code = failure_code if status == "failed" else None
            self.conn.execute(
                "UPDATE payments SET status = ?, failure_code = ?, note = COALESCE(?, note), updated_at = ? WHERE id = ?",
                (status, code, note, now, payment_id),
            )
            self.conn.execute("INSERT INTO payment_events (payment_id, ts, status, failure_code, note) VALUES (?, ?, ?, ?, ?)",
                              (payment_id, now, status, code, note))
            self.conn.commit()
            return self._payment_view(payment_id)

    def link_grievance(self, payment_id: int, grievance_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE payments SET grievance_id = ? WHERE id = ?", (grievance_id, payment_id))
            self.conn.commit()

    def _payment_view(self, payment_id: int) -> Dict[str, Any]:
        row = self.conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
        if not row:
            raise NotFound("Payment not found")
        view = dict(row)
        view["timeline"] = [dict(e) for e in self.conn.execute(
            "SELECT ts, status, failure_code, note FROM payment_events WHERE payment_id = ? ORDER BY id", (payment_id,)
        ).fetchall()]
        waiting = 0 if row["status"] == "paid" else max((self._today() - date.fromisoformat(row["sold_on"])).days, 0)
        view["days_waiting"] = waiting
        view["can_escalate"] = row["grievance_id"] is None and (
            row["status"] == "failed" or (row["status"] in ("pending", "processing") and waiting >= ESCALATE_AFTER_DAYS))
        slot = self.conn.execute(
            "SELECT s.slot_date, m.name AS mandi_name FROM slots s LEFT JOIN mandis m ON m.id = s.mandi_id WHERE s.id = ?",
            (row["slot_id"],)).fetchone() if row["slot_id"] else None
        view["mandi_name"] = slot["mandi_name"] if slot else None
        owner = self.conn.execute("SELECT name FROM farmer_registry WHERE farmer_id = ?", (row["farmer_id"],)).fetchone()
        view["farmer_name"] = owner["name"] if owner else None
        return view

    def get_payment(self, payment_id: int) -> Dict[str, Any]:
        with self.lock:
            return self._payment_view(payment_id)

    def farmer_payments(self, farmer_id: str) -> List[Dict[str, Any]]:
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM payments WHERE farmer_id = ? ORDER BY sold_on DESC, id DESC", (farmer_id,)).fetchall()]
            return [self._payment_view(i) for i in ids]

    def admin_payments(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        query, params = "SELECT id FROM payments", []
        if status:
            query += " WHERE status = ?"; params.append(status)
        query += " ORDER BY updated_at DESC, id DESC LIMIT ?"; params.append(limit)
        with self.lock:
            return [self._payment_view(r["id"]) for r in self.conn.execute(query, params).fetchall()]

    # ---------- notifications and preferences ----------

    def add_notification(self, farmer_id: str, kind: str, title: str, body: str, sms_status: str = "none",
                         sms_detail: Optional[str] = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO notifications (farmer_id, kind, title, body, sms_status, sms_detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (farmer_id, kind, title, body, sms_status, sms_detail, self._iso_now()),
            )
            self.conn.commit()
            return cur.lastrowid

    def set_sms_result(self, notification_id: int, status: str, detail: Optional[str]) -> None:
        with self.lock:
            self.conn.execute("UPDATE notifications SET sms_status = ?, sms_detail = ? WHERE id = ?",
                              (status, detail, notification_id))
            self.conn.commit()

    def farmer_notifications(self, farmer_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, kind, title, body, created_at, read_at FROM notifications WHERE farmer_id = ? ORDER BY id DESC LIMIT ?",
                (farmer_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_notifications_read(self, farmer_id: str) -> int:
        with self.lock:
            cur = self.conn.execute("UPDATE notifications SET read_at = ? WHERE farmer_id = ? AND read_at IS NULL",
                                    (self._iso_now(), farmer_id))
            self.conn.commit()
            return cur.rowcount

    def admin_notifications(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT n.id, n.farmer_id, f.name AS farmer_name, n.kind, n.title, n.body, n.sms_status, n.sms_detail, "
                "n.created_at, n.read_at FROM notifications n LEFT JOIN farmer_registry f ON f.farmer_id = n.farmer_id "
                "ORDER BY n.id DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_language(self, farmer_id: str) -> Optional[str]:
        with self.lock:
            row = self.conn.execute("SELECT language FROM farmer_prefs WHERE farmer_id = ?", (farmer_id,)).fetchone()
        return row["language"] if row else None

    def all_languages(self) -> Dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT farmer_id, language FROM farmer_prefs WHERE language IS NOT NULL").fetchall()
        return {r["farmer_id"]: r["language"] for r in rows}

    def set_language(self, farmer_id: str, language: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO farmer_prefs (farmer_id, language) VALUES (?, ?) ON CONFLICT(farmer_id) DO UPDATE SET language = excluded.language",
                (farmer_id, language))
            self.conn.commit()

    # ---------- farmer registry (who has registered in the app) ----------

    @staticmethod
    def _registry_row(row) -> Dict[str, Any]:
        return {
            "farmer_id": row["farmer_id"], "name": row["name"],
            "primary_crops": json.loads(row["primary_crops"] or "[]"),
            "registered_at": row["registered_at"], "updated_at": row["updated_at"],
            "district": row["district"], "state": row["state"],
        }

    def upsert_farmer(self, farmer_id: str, name: str, primary_crops: List[str],
                      district: Optional[str] = None, state: Optional[str] = None) -> Dict[str, Any]:
        now = self._iso_now()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO farmer_registry (farmer_id, name, primary_crops, registered_at, updated_at, district, state)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(farmer_id) DO UPDATE SET
                    name = excluded.name, primary_crops = excluded.primary_crops, updated_at = excluded.updated_at,
                    district = COALESCE(excluded.district, district), state = COALESCE(excluded.state, state)
                """,
                (farmer_id, name, json.dumps(primary_crops), now, now, district, state),
            )
            self.conn.commit()
            return self._registry_row(self.conn.execute("SELECT * FROM farmer_registry WHERE farmer_id = ?", (farmer_id,)).fetchone())

    def get_registered(self, farmer_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM farmer_registry WHERE farmer_id = ?", (farmer_id,)).fetchone()
        return self._registry_row(row) if row else None

    def list_registered(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM farmer_registry ORDER BY updated_at DESC").fetchall()
        return [self._registry_row(r) for r in rows]

    def erase_farmer(self, farmer_id: str) -> Dict[str, int]:
        """Deletes every row this store holds for a farmer. Chat history is erased by the memory module."""
        with self.lock:
            payment_ids = [r["id"] for r in self.conn.execute("SELECT id FROM payments WHERE farmer_id = ?", (farmer_id,)).fetchall()]
            events = 0
            for pid in payment_ids:
                events += self.conn.execute("DELETE FROM payment_events WHERE payment_id = ?", (pid,)).rowcount
            counts = {
                "payment_events": events,
                "payments": self.conn.execute("DELETE FROM payments WHERE farmer_id = ?", (farmer_id,)).rowcount,
                "slots": self.conn.execute("DELETE FROM slots WHERE farmer_id = ?", (farmer_id,)).rowcount,
                "notifications": self.conn.execute("DELETE FROM notifications WHERE farmer_id = ?", (farmer_id,)).rowcount,
                "preferences": self.conn.execute("DELETE FROM farmer_prefs WHERE farmer_id = ?", (farmer_id,)).rowcount,
                "registry": self.conn.execute("DELETE FROM farmer_registry WHERE farmer_id = ?", (farmer_id,)).rowcount,
            }
            self.conn.commit()
        return counts

    # ---------- dashboard summary ----------

    def stats(self) -> Dict[str, Any]:
        today = self._today().isoformat()
        with self.lock:
            one = lambda q, p=(): self.conn.execute(q, p).fetchone()["c"]
            total = lambda q, p=(): float(self.conn.execute(q, p).fetchone()["c"])
            return {
                "slots_today": one("SELECT COUNT(*) AS c FROM slots WHERE slot_date = ? AND status != 'cancelled'", (today,)),
                "queue_active": one("SELECT COUNT(*) AS c FROM slots WHERE slot_date = ? AND status IN ('booked','checked_in')", (today,)),
                "payments_pending": one("SELECT COUNT(*) AS c FROM payments WHERE status IN ('pending','processing')"),
                "payments_failed": one("SELECT COUNT(*) AS c FROM payments WHERE status = 'failed'"),
                "payments_paid": one("SELECT COUNT(*) AS c FROM payments WHERE status = 'paid'"),
                "payments_overdue": one(
                    "SELECT COUNT(*) AS c FROM payments WHERE status IN ('pending','processing') AND sold_on <= ?",
                    ((self._today() - timedelta(days=ESCALATE_AFTER_DAYS)).isoformat(),)),
                "amount_paid": total("SELECT COALESCE(SUM(amount), 0) AS c FROM payments WHERE status = 'paid'"),
                "amount_pending": total("SELECT COALESCE(SUM(amount), 0) AS c FROM payments WHERE status IN ('pending','processing')"),
                "amount_failed": total("SELECT COALESCE(SUM(amount), 0) AS c FROM payments WHERE status = 'failed'"),
                "no_shows_today": one("SELECT COUNT(*) AS c FROM slots WHERE slot_date = ? AND status = 'no_show'", (today,)),
                "completed_today": one("SELECT COUNT(*) AS c FROM slots WHERE slot_date = ? AND status = 'completed'", (today,)),
            }
