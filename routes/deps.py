from dataclasses import dataclass
from typing import Any, Callable, Optional

ClassifyFn = Callable[[Optional[str], Optional[bytes], str], Optional[str]]
AnswerFn = Callable[[str, str, str], str]


@dataclass
class Deps:
    memory: Any
    store: Any
    notifications: Any
    check_admin: Callable[[Optional[str]], None]
    classify_issue: Optional[ClassifyFn] = None
    answer: Optional[AnswerFn] = None
    ivr_auth_token: Optional[str] = None
    public_base_url: Optional[str] = None
    ivr_language: str = "hi-IN"
    ivr_allow_unsigned: bool = False
