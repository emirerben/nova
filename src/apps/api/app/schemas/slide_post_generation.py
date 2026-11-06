"""Public identity of one durable SlidePost generation request."""

import uuid
from typing import Literal

from pydantic import BaseModel


class SlidePostGenerationReceiptOut(BaseModel):
    client_request_id: uuid.UUID
    item_id: uuid.UUID
    draft_version: int
    job_id: uuid.UUID
    dispatch_state: Literal["unconfirmed", "published", "publish_failed"]
    job_status: str | None
    is_current_target: bool
