"""
Core memory module: three layers, not one plain vector search.

1. STRUCTURED FILTER (SQL) -- same farmer, recency window, optional
   crop/season -- exact, cheap, no AI involved.
2. SEMANTIC RE-RANKING -- within that already-small filtered set, rank by
   real similarity to the new query, blended slightly with recency so a
   very old but still-relevant match doesn't always lose to a
   barely-relevant recent one.
3. CONSOLIDATED PROFILE -- a periodically-rebuilt short summary (crops,
   recurring topics), returned on every call basically for free, so the
   system doesn't need to re-derive stable facts from raw history each time.

Crop season (Kharif/Rabi/Zaid) is treated as a hard SQL filter, not a
semantic dimension, because it's a fixed real-world category a pure
embedding search can't reliably reason about on its own.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Callable
from .models import Interaction, FarmerProfile, season_for_date


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass
class RetrievalContext:
    relevant_interactions: List[Interaction] = field(default_factory=list)
    profile: Optional[FarmerProfile] = None

    def as_prompt_text(self) -> str:
        parts = []
        if self.profile and self.profile.summary_text:
            parts.append(f"Farmer profile summary: {self.profile.summary_text}")
        if self.relevant_interactions:
            parts.append("Relevant past conversations:")
            for it in self.relevant_interactions:
                parts.append(f"- [{it.timestamp.date()}] Farmer asked: {it.query_text}\n  Assistant replied: {it.response_text}")
        return "\n".join(parts)


class FarmerMemoryModule:
    def __init__(self, storage, embed_fn: Optional[Callable[[str], List[float]]] = None):
        self.storage = storage
        if embed_fn is None:
            from .embeddings import embed_text
            embed_fn = embed_text
        self.embed_fn = embed_fn

    def log_interaction(
        self,
        farmer_id: str,
        query_text: str,
        response_text: str,
        crop: Optional[str] = None,
        crop_season: Optional[str] = None,
        intent: Optional[str] = None,
        channel: Optional[str] = None,
        timestamp: Optional[datetime] = None,
        is_grievance: bool = False,
        embed: bool = True,
    ) -> int:
        """Call this once per conversation turn. Every interaction gets logged --
        the retrieval step, not the logging step, decides what's relevant later."""
        ts = timestamp or datetime.utcnow()
        interaction = Interaction(
            farmer_id=farmer_id,
            timestamp=ts,
            query_text=query_text,
            response_text=response_text,
            crop=crop,
            crop_season=crop_season,  # auto-filled from ts if None, see models.py
            intent=intent,
            channel=channel,
            embedding=self.embed_fn(query_text) if embed else None,
            is_grievance=is_grievance,
        )
        return self.storage.save_interaction(interaction)

    def retrieve_context(
        self,
        farmer_id: str,
        query_text: str,
        lookback_months: int = 24,
        crop_season: Optional[str] = None,
        crop: Optional[str] = None,
        top_k: int = 3,
        min_similarity: float = 0.30,
        recency_half_life_days: int = 180,
    ) -> RetrievalContext:
        since = datetime.utcnow() - timedelta(days=lookback_months * 30)
        candidates = self.storage.fetch_candidates(farmer_id, since, crop_season=crop_season, crop=crop)

        query_emb = self.embed_fn(query_text)
        scored = []
        now = datetime.utcnow()
        for c in candidates:
            sim = cosine_similarity(query_emb, c.embedding or [])
            if sim < min_similarity:
                continue
            age_days = max((now - c.timestamp).days, 0)
            recency_weight = 0.5 ** (age_days / recency_half_life_days)
            blended = sim * (0.5 + 0.5 * recency_weight)
            scored.append((blended, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = [c for _, c in scored[:top_k]]

        profile = self.storage.get_profile(farmer_id)
        return RetrievalContext(relevant_interactions=top, profile=profile)

    def consolidate_profile(self, farmer_id: str) -> FarmerProfile:
        """Call this periodically per farmer (cron / scheduled job), not on
        every chat -- rebuilds the cheap summary from full raw history."""
        since = datetime.utcnow() - timedelta(days=365 * 3)
        history = self.storage.all_interactions_since(farmer_id, since)
        crops = sorted({h.crop for h in history if h.crop})
        summary = f"Farmer has discussed: {', '.join(crops)}." if crops else "No recurring crop pattern yet."
        profile = FarmerProfile(
            farmer_id=farmer_id,
            summary_text=summary,
            structured_facts={"crops": crops, "interaction_count": len(history)},
            last_updated=datetime.utcnow(),
        )
        self.storage.save_profile(profile)
        return profile

    def forget_farmer(self, farmer_id: str) -> int:
        """DPDP Act right-to-erasure -- deletes all interactions and the profile."""
        return self.storage.delete_farmer(farmer_id)

    # ---------------- admin/dashboard read path ----------------
    # Thin pass-throughs to storage -- kept here so api.py only ever talks
    # to FarmerMemoryModule, never reaches into storage directly, same
    # pattern as log_interaction/retrieve_context above.

    def admin_overview(self) -> dict:
        return self.storage.admin_overview_stats()

    def admin_trends(self, days: int = 14) -> list:
        return self.storage.admin_trends(days)

    def admin_recent_activity(self, limit: int = 12) -> list:
        return self.storage.admin_recent_activity(limit)

    def admin_farmers(self, search: Optional[str] = None) -> list:
        return self.storage.admin_list_farmers(search)

    def admin_farmer_history(self, farmer_id: str) -> list:
        return self.storage.admin_farmer_history(farmer_id)

    def admin_grievances(self, status: Optional[str] = None) -> list:
        return self.storage.admin_list_grievances(status)

    def admin_update_grievance(self, interaction_id: int, status: str, note: Optional[str]) -> bool:
        return self.storage.admin_update_grievance(interaction_id, status, note)

    def get_grievance(self, interaction_id: int) -> Optional[dict]:
        return self.storage.get_grievance(interaction_id)

    def farmer_grievances(self, farmer_id: str) -> list:
        return self.storage.farmer_grievances(farmer_id)
