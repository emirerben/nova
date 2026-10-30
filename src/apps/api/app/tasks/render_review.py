"""Evidence preparation only. No paid review or edit authority."""

import uuid

from app.worker import celery_app


@celery_app.task(
    name="tasks.prepare_generation_review", soft_time_limit=105, time_limit=115, max_retries=0
)
def prepare_generation_review(review_id: str):
    from app.services.render_review_admission import run_generation_review

    return {"review_id": review_id, "status": run_generation_review(uuid.UUID(review_id))}
