"""Shared provider-owned public research evidence; never model-authored links."""

import ipaddress
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator


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
