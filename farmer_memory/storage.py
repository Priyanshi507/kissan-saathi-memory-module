"""
Storage layer for the Farmer Memory Module.

Design note (this is the part worth explaining in your PPT/demo):
----------------------------------------------------------------
This module defines a small Storage INTERFACE (save/query methods) with
one concrete implementation, SQLiteStorage, used here so the whole module
runs with zero setup for your demo/judging round.

In production, swap SQLiteStorage for a PostgresStorage that talks to
Postgres with the pgvector extension -- same interface, so nothing in
memory_module.py has to change. The equivalent production schema is:

    CREATE EXTENSION IF NOT EXISTS vector;

    CREATE TABLE interactions (
        id           BIGSERIAL PRIMARY KEY,
        farmer_id    TEXT NOT NULL,
        ts           TIMESTAMPTZ NOT NULL,
        crop         TEXT,
        crop_season  TEXT,
        intent       TEXT,
        channel      TEXT,
        query_text   TEXT NOT NULL,
        response_text TEXT NOT NULL,
        embedding    VECTOR(384)          -- dimension depends on your embedding model
    );
    CREATE INDEX ON interactions (farmer_id, ts DESC);
    CREATE INDEX ON interactions (farmer_id, crop_season);
    CREATE INDEX ON interactions USING ivfflat (embedding vector_cosine_ops);

    CREATE TABLE farmer_profiles (
        farmer_id       TEXT PRIMARY KEY,
        summary_text    TEXT,
        structured_facts JSONB,
        last_updated    TIMESTAMPTZ
    );

Using Postgres + pgvector instead of a separate vector DB (Pinecone/
Weaviate/Qdrant) means structured filtering (farmer_id, crop_season,
date range) and semantic similarity happen in ONE SQL query instead of
two round trips across two systems -- simpler ops, one source of truth,
and it's a database you likely already run.
"""

import sqlite3
import json
import threading
from datetime import datetime, timedelta
from typing import List, Optional
from .models import Interaction, FarmerProfile


class SQLiteStorage:
    def __init__(self, db_path: str = ":memory:"):
        # check_same_thread=False + an explicit lock: a web server (FastAPI,
        # Flask) handles each request in a different worker thread, and a
        # plain sqlite3 connection is not safe to share across threads
        # without both of these -- found by actually running this behind
        # uvicorn, not by the unit tests (which never left one thread).
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    @property
    def lock(self):
        return self._lock

    def _init_schema(self):
        with self._lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    farmer_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    crop TEXT,
                    crop_season TEXT,
                    intent TEXT,
                    channel TEXT,
                    query_text TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    embedding TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_farmer_ts
                    ON interactions (farmer_id, ts DESC);
                CREATE INDEX IF NOT EXISTS idx_farmer_season
                    ON interactions (farmer_id, crop_season);

                CREATE TABLE IF NOT EXISTS farmer_profiles (
                    farmer_id TEXT PRIMARY KEY,
                    summary_text TEXT,
                    structured_facts TEXT,
                    last_updated TEXT
                );
                """
            )
            self.conn.commit()
            # ALTER TABLE ... ADD COLUMN, not part of CREATE TABLE IF NOT
            # EXISTS above, because that clause has no effect on a table
            # that already exists from a previous run of the app -- these
            # columns are newer than the original schema, so existing
            # databases need them added explicitly. Each wrapped
            # individually since SQLite has no "ADD COLUMN IF NOT EXISTS"
            # and re-adding an existing column errors.
            for col_def in (
                "is_grievance INTEGER DEFAULT 0",
                "grievance_status TEXT DEFAULT 'new'",
                "grievance_note TEXT",
            ):
                try:
                    self.conn.execute(f"ALTER TABLE interactions ADD COLUMN {col_def}")
                    self.conn.commit()
                except sqlite3.OperationalError:
                    pass  # column already exists from a prior run

    # ---------- interactions ----------

    def save_interaction(self, interaction: Interaction) -> int:
        with self._lock:
            cur = self.conn.execute(
                """
                INSERT INTO interactions
                    (farmer_id, ts, crop, crop_season, intent, channel,
                     query_text, response_text, embedding,
                     is_grievance, grievance_status, grievance_note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    interaction.farmer_id,
                    interaction.timestamp.isoformat(),
                    interaction.crop,
                    interaction.crop_season,
                    interaction.intent,
                    interaction.channel,
                    interaction.query_text,
                    interaction.response_text,
                    json.dumps(interaction.embedding) if interaction.embedding else None,
                    int(interaction.is_grievance),
                    interaction.grievance_status,
                    interaction.grievance_note,
                ),
            )
            self.conn.commit()
            return cur.lastrowid

    def fetch_candidates(
        self,
        farmer_id: str,
        since: datetime,
        crop_season: Optional[str] = None,
        crop: Optional[str] = None,
    ) -> List[Interaction]:
        """
        Structured pre-filter: same farmer, within the lookback window,
        optionally narrowed to a specific season/crop. This runs BEFORE
        semantic ranking, so vector search only has to rank a small,
        already-relevant candidate set instead of the farmer's entire history.
        """
        query = "SELECT * FROM interactions WHERE farmer_id = ? AND ts >= ?"
        params = [farmer_id, since.isoformat()]
        if crop_season:
            query += " AND crop_season = ?"
            params.append(crop_season)
        if crop:
            query += " AND crop = ?"
            params.append(crop)
        query += " ORDER BY ts DESC"

        with self._lock:
            rows = self.conn.execute(query, params).fetchall()

        results = []
        for r in rows:
            results.append(
                Interaction(
                    id=r["id"],
                    farmer_id=r["farmer_id"],
                    timestamp=datetime.fromisoformat(r["ts"]),
                    crop=r["crop"],
                    crop_season=r["crop_season"],
                    intent=r["intent"],
                    channel=r["channel"],
                    query_text=r["query_text"],
                    response_text=r["response_text"],
                    embedding=json.loads(r["embedding"]) if r["embedding"] else None,
                    is_grievance=bool(r["is_grievance"]) if "is_grievance" in r.keys() else False,
                    grievance_status=r["grievance_status"] if "grievance_status" in r.keys() and r["grievance_status"] else "new",
                    grievance_note=r["grievance_note"] if "grievance_note" in r.keys() else None,
                )
            )
        return results

    def all_interactions_since(self, farmer_id: str, since: datetime) -> List[Interaction]:
        return self.fetch_candidates(farmer_id, since)

    # ---------- admin/dashboard queries ----------
    # These are read-mostly queries over the SAME interactions table --
    # nothing new is being collected here, this is just a different way
    # of looking at data the app already logs on every /chat call.

    def admin_overview_stats(self) -> dict:
        with self._lock:
            total_farmers = self.conn.execute(
                "SELECT COUNT(DISTINCT farmer_id) AS c FROM interactions"
            ).fetchone()["c"]
            total_interactions = self.conn.execute(
                "SELECT COUNT(*) AS c FROM interactions"
            ).fetchone()["c"]
            today = datetime.utcnow().date().isoformat()
            interactions_today = self.conn.execute(
                "SELECT COUNT(*) AS c FROM interactions WHERE ts >= ?", (today,)
            ).fetchone()["c"]
            open_grievances = self.conn.execute(
                "SELECT COUNT(*) AS c FROM interactions WHERE is_grievance = 1 AND grievance_status != 'resolved'"
            ).fetchone()["c"]
            total_grievances = self.conn.execute(
                "SELECT COUNT(*) AS c FROM interactions WHERE is_grievance = 1"
            ).fetchone()["c"]
            by_crop = self.conn.execute(
                "SELECT crop, COUNT(*) AS c FROM interactions WHERE crop IS NOT NULL GROUP BY crop ORDER BY c DESC LIMIT 10"
            ).fetchall()
            avg_response_len = self.conn.execute(
                "SELECT AVG(LENGTH(response_text)) AS avg_len FROM interactions"
            ).fetchone()["avg_len"]
        return {
            "total_farmers": total_farmers,
            "total_interactions": total_interactions,
            "interactions_today": interactions_today,
            "open_grievances": open_grievances,
            "total_grievances": total_grievances,
            "by_crop": [{"crop": r["crop"], "count": r["c"]} for r in by_crop],
            "avg_response_chars": round(avg_response_len) if avg_response_len else 0,
        }

    def admin_recent_activity(self, limit: int = 12) -> List[dict]:
        """Most recent interactions across ALL farmers -- powers the live
        activity feed. Real data, no aggregation, just the latest rows."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, farmer_id, ts, crop, query_text, is_grievance FROM interactions ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def admin_trends(self, days: int = 14) -> List[dict]:
        """Daily interaction and grievance counts for the last N days --
        real, computed from actual timestamps, powers the trend chart."""
        since = (datetime.utcnow() - timedelta(days=days - 1)).date()
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT substr(ts, 1, 10) AS day,
                       COUNT(*) AS total,
                       SUM(is_grievance) AS grievances
                FROM interactions
                WHERE ts >= ?
                GROUP BY day
                ORDER BY day ASC
                """,
                (since.isoformat(),),
            ).fetchall()
        by_day = {r["day"]: {"total": r["total"], "grievances": r["grievances"] or 0} for r in rows}
        # Fill in zero-days so the chart doesn't have gaps for quiet days
        result = []
        for i in range(days):
            day = (since + timedelta(days=i)).isoformat()
            entry = by_day.get(day, {"total": 0, "grievances": 0})
            result.append({"date": day, "total": entry["total"], "grievances": entry["grievances"]})
        return result

    def admin_list_farmers(self, search: Optional[str] = None) -> List[dict]:
        query = """
            SELECT farmer_id,
                   COUNT(*) AS message_count,
                   MAX(ts) AS last_active,
                   MIN(ts) AS first_seen,
                   SUM(is_grievance) AS grievance_count,
                   GROUP_CONCAT(DISTINCT crop) AS crops
            FROM interactions
        """
        params: list = []
        if search:
            query += " WHERE farmer_id LIKE ?"
            params.append(f"%{search}%")
        query += " GROUP BY farmer_id ORDER BY last_active DESC"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [
            {
                "farmer_id": r["farmer_id"],
                "message_count": r["message_count"],
                "last_active": r["last_active"],
                "first_seen": r["first_seen"],
                "grievance_count": r["grievance_count"] or 0,
                "crops": [c for c in (r["crops"] or "").split(",") if c],
            }
            for r in rows
        ]

    def admin_farmer_history(self, farmer_id: str) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM interactions WHERE farmer_id = ? ORDER BY ts DESC",
                (farmer_id,),
            ).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "embedding"} for r in rows]

    def admin_list_grievances(self, status: Optional[str] = None) -> List[dict]:
        query = "SELECT * FROM interactions WHERE is_grievance = 1"
        params: list = []
        if status:
            query += " AND grievance_status = ?"
            params.append(status)
        query += " ORDER BY ts DESC"
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [{k: v for k, v in dict(r).items() if k != "embedding"} for r in rows]

    def get_grievance(self, interaction_id: int) -> Optional[dict]:
        with self._lock:
            row = self.conn.execute(
                "SELECT id, farmer_id, ts, query_text, grievance_status, grievance_note "
                "FROM interactions WHERE id = ? AND is_grievance = 1",
                (interaction_id,),
            ).fetchone()
        return dict(row) if row else None

    def farmer_grievances(self, farmer_id: str) -> List[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, ts, query_text, grievance_status, grievance_note "
                "FROM interactions WHERE farmer_id = ? AND is_grievance = 1 ORDER BY ts DESC",
                (farmer_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def admin_update_grievance(self, interaction_id: int, status: str, note: Optional[str]) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE interactions SET grievance_status = ?, grievance_note = ? WHERE id = ? AND is_grievance = 1",
                (status, note, interaction_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    # ---------- profile ----------

    def get_profile(self, farmer_id: str) -> Optional[FarmerProfile]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM farmer_profiles WHERE farmer_id = ?", (farmer_id,)
            ).fetchone()
        if not row:
            return None
        return FarmerProfile(
            farmer_id=row["farmer_id"],
            summary_text=row["summary_text"] or "",
            structured_facts=json.loads(row["structured_facts"]) if row["structured_facts"] else {},
            last_updated=datetime.fromisoformat(row["last_updated"]) if row["last_updated"] else None,
        )

    def save_profile(self, profile: FarmerProfile):
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO farmer_profiles (farmer_id, summary_text, structured_facts, last_updated)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(farmer_id) DO UPDATE SET
                    summary_text = excluded.summary_text,
                    structured_facts = excluded.structured_facts,
                    last_updated = excluded.last_updated
                """,
                (
                    profile.farmer_id,
                    profile.summary_text,
                    json.dumps(profile.structured_facts),
                    profile.last_updated.isoformat() if profile.last_updated else None,
                ),
            )
            self.conn.commit()

    def delete_farmer(self, farmer_id: str) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM interactions WHERE farmer_id = ?", (farmer_id,))
            self.conn.execute("DELETE FROM farmer_profiles WHERE farmer_id = ?", (farmer_id,))
            self.conn.commit()
            return cur.rowcount
