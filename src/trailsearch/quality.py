"""
Content quality helpers used by crawler stage routing and Tavily-like responses.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]+")
_BOILERPLATE_TERMS = {
    "advertisement",
    "cookie",
    "cookies",
    "login",
    "newsletter",
    "privacy policy",
    "sign in",
    "sign up",
    "subscribe",
    "terms of service",
}


@dataclass(frozen=True)
class ContentQuality:
    """A lightweight production quality estimate for extracted content."""

    usable: bool
    score: float
    length: int
    query_overlap: float
    boilerplate_penalty: float
    reasons: list[str]


def normalize_text_length(content: str | None) -> int:
    """Measure content length without whitespace noise."""
    if not content:
        return 0
    return len(re.sub(r"\s+", "", content))


def tokenize(text: str) -> list[str]:
    """Tokenize Latin words/numbers and contiguous CJK spans for rough matching."""
    return [token.lower() for token in _TOKEN_PATTERN.findall(text or "")]


def _query_overlap(query: str, content: str) -> float:
    query_terms = {token for token in tokenize(query) if len(token) > 1}
    if not query_terms:
        return 1.0

    content_terms = set(tokenize(content))
    if not content_terms:
        return 0.0

    return len(query_terms & content_terms) / len(query_terms)


def _boilerplate_penalty(content: str) -> float:
    normalized = re.sub(r"\s+", " ", content or "").lower()
    if not normalized:
        return 1.0

    hits = sum(1 for term in _BOILERPLATE_TERMS if term in normalized)
    line_count = max(len([line for line in content.splitlines() if line.strip()]), 1)
    repeated_line_count = len(content.splitlines()) - len(set(content.splitlines()))
    repeated_penalty = min(repeated_line_count / line_count, 0.4)
    term_penalty = min(hits * 0.08, 0.4)
    return round(min(term_penalty + repeated_penalty, 1.0), 4)


def assess_content_quality(
    content: str | None,
    query: str = "",
    min_content_length: int = 300,
    min_score: float = 0.35,
) -> ContentQuality:
    """
    Estimate whether extracted content is good enough for downstream search/RAG.

    The benchmark has gold snippets, but production traffic usually does not. This heuristic
    keeps the hard gates simple: enough text, not mostly boilerplate, and at least weak query
    alignment when a meaningful query is available.
    """
    text = content or ""
    length = normalize_text_length(text)
    overlap = _query_overlap(query, text)
    boilerplate = _boilerplate_penalty(text)
    length_score = min(math.log1p(length) / math.log1p(max(min_content_length * 4, 1)), 1.0)
    score = round((0.55 * length_score) + (0.3 * overlap) + (0.15 * (1 - boilerplate)), 4)

    reasons: list[str] = []
    if length < min_content_length:
        reasons.append("short_content")
    if boilerplate >= 0.45:
        reasons.append("boilerplate_heavy")
    if tokenize(query) and overlap < 0.08:
        reasons.append("low_query_overlap")
    if score < min_score:
        reasons.append("low_quality_score")

    return ContentQuality(
        usable=not reasons,
        score=score,
        length=length,
        query_overlap=round(overlap, 4),
        boilerplate_penalty=boilerplate,
        reasons=reasons,
    )


def chunk_text(content: str, max_chars: int = 1600, overlap_chars: int = 180) -> list[str]:
    """Split content into stable, sentence-ish chunks for LLM context."""
    text = re.sub(r"\n{3,}", "\n\n", (content or "").strip())
    if not text:
        return []

    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue

        if current:
            chunks.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
            continue

        start = 0
        while start < len(paragraph):
            chunks.append(paragraph[start : start + max_chars].strip())
            start += max_chars - overlap_chars
        current = ""

    if current:
        chunks.append(current)
    return chunks
