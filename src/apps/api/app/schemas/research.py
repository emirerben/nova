"""Shared provider-owned public research evidence; never model-authored links."""

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResearchSource(BaseModel):
    title: str = Field(max_length=300)
    url: str = Field(max_length=2048)

    @field_validator("url")
    @classmethod
    def public_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or not host
            or "." not in host
            or host.endswith((".local", ".internal", ".localhost"))
        ):
            raise ValueError("A public source link is required")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Provider links are displayed, never fetched by our backend. Numeric
            # browser aliases (127.1 / decimal IPs) are not public citations either.
            if all(part.isdigit() for part in host.split(".")):
                raise ValueError("Numeric source address") from None
        else:
            if not address.is_global:
                raise ValueError("Private source address")
        return value


_CLAIM_STOPWORDS = frozenset(
    "the and for was were are with from that this its has had have been into over after "
    "before about which their they them than then also not but who whom what when where".split()
)


def claim_tokens(text) -> frozenset[str]:
    # Fact ids are positional and a fresh grounded search rewords the same claim, so
    # identity is the distinctive words (names, numbers), not an exact hash.
    words = "".join(c if c.isalnum() else " " for c in str(text).casefold()).split()
    return frozenset(w for w in words if (len(w) > 2 or w.isdigit()) and w not in _CLAIM_STOPWORDS)


def is_same_claim(text, rejected_texts, threshold=0.75) -> bool:
    """True when `text` repeats a rejected claim, allowing light rewording."""
    tokens = claim_tokens(text)
    for other in map(claim_tokens, rejected_texts):
        shared = len(tokens & other)
        if shared >= 3 and shared / min(len(tokens), len(other)) >= threshold:
            return True
    return False


class ResearchFact(BaseModel):
    id: str
    text: str = Field(min_length=1, max_length=3000)
    sources: list[ResearchSource] = Field(min_length=1, max_length=8)


class ResearchEvidence(BaseModel):
    facts: list[ResearchFact] = Field(min_length=1, max_length=30)
    retrieved_at: str
    search_entry_html: str = Field(max_length=100000)


class StoryResearch(BaseModel):
    topic: str = Field(min_length=3, max_length=200)
    basis: Literal["background_context"] = "background_context"
    evidence: ResearchEvidence
    reviewed_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_current: bool = True


class PreparedResearchText(BaseModel):
    """One sourced explanation the creator saw before approving (KRI-881)."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=180)
    media_ids: list[str] = Field(min_length=1, max_length=50)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    sources: list[ResearchSource] = Field(min_length=1, max_length=16)
    facts: list[ResearchFact] = Field(min_length=1, max_length=5)


class PreparedResearch(BaseModel):
    """Research done before approval and pinned to it (KRI-881).

    Server-owned: written by the planning step from provider-owned grounding, carried in
    the approved brief binding (so its digest covers it) and replayed by the render,
    which never searches or writes it again. ``failed`` carries the precise reason the
    creator was told before approval; it is never pinned to an approval.
    """

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    status: Literal["ready", "failed"]
    requirement_id: str = Field(min_length=1, max_length=24)
    topic: str = Field(min_length=3, max_length=500)
    request_quote: str = Field(min_length=4, max_length=400)
    basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    texts: list[PreparedResearchText] = Field(default_factory=list, max_length=12)
    retrieved_at: str = ""
    # One copy of the provider's search attribution for every explanation (F12).
    search_entry_html: str = Field(default="", max_length=100000)
    limitations: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)
