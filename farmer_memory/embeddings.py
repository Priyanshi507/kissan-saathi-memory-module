"""
A tiny, dependency-free stub embedding used only when no GOOGLE_API_KEY is
set -- lets the whole pipeline (retrieval, logging, scoring) be tested and
demoed without a real key. NOT semantically meaningful beyond literal word
overlap -- swapped for gemini-embedding-001 automatically once a real key
is present (see api.py).
"""
import hashlib
import re
from typing import List

VECTOR_DIM = 256


def embed_text(text: str) -> List[float]:
    vec = [0.0] * VECTOR_DIM
    words = re.findall(r"[a-z0-9]+", text.lower())
    if not words:
        return vec
    for word in words:
        idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % VECTOR_DIM
        vec[idx] += 1.0
    norm = sum(w * w for w in vec) ** 0.5
    if norm > 0:
        vec = [w / norm for w in vec]
    return vec
