"""
Data models for the Farmer Memory Module.

Two kinds of records, mirroring how human memory actually works:

- Interaction  -> "episodic memory": one raw logged conversation turn.
- FarmerProfile -> "semantic memory": a periodically-updated distilled
  summary of what the system has learned about a farmer over time, so
  the system doesn't have to re-search raw history for stable facts
  (land size, usual crops, village, recurring issues) on every query.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any


# India's agricultural year has three cropping seasons. Treating this as a
# fixed enum (rather than free text) is what lets us do exact structured
# filtering instead of relying on semantic search to "understand" seasons.
CROP_SEASONS = ("Kharif", "Rabi", "Zaid")


def season_for_date(dt: datetime) -> str:
    """
    Best-effort mapping of a calendar date to an Indian crop season.
    Kharif: Jun-Oct (monsoon sown)
    Rabi:   Nov-Mar (winter sown)
    Zaid:   Apr-May (short summer season)
    Callers can always override this with an explicit season if the
    farmer's actual sowing record differs from the calendar default.
    """
    month = dt.month
    if month in (6, 7, 8, 9, 10):
        return "Kharif"
    if month in (11, 12, 1, 2, 3):
        return "Rabi"
    return "Zaid"


@dataclass
class Interaction:
    """One logged conversation turn."""
    farmer_id: str
    timestamp: datetime
    query_text: str
    response_text: str
    crop: Optional[str] = None
    crop_season: Optional[str] = None       # Kharif / Rabi / Zaid
    intent: Optional[str] = None            # e.g. procurement_status, price_query, pest_query
    channel: Optional[str] = None           # app / sms / ivr / web
    embedding: Optional[List[float]] = None
    is_grievance: bool = False              # flagged by the assistant as a reported problem, not a question
    grievance_status: str = "new"           # new / in_progress / resolved -- only meaningful when is_grievance
    grievance_note: Optional[str] = None    # staff notes on what action was taken
    id: Optional[int] = None

    def __post_init__(self):
        if self.crop_season is None:
            self.crop_season = season_for_date(self.timestamp)


@dataclass
class FarmerProfile:
    """
    The consolidated, always-cheap-to-fetch summary of a farmer.
    Updated periodically by consolidate_profile(), not on every query.
    """
    farmer_id: str
    summary_text: str = ""
    structured_facts: Dict[str, Any] = field(default_factory=dict)
    last_updated: Optional[datetime] = None
