"""Evidence preparation only. No paid review or edit authority."""

import uuid

from app.worker import celery_app

SWEEP_BUDGET_S = 150


@celery_app.task(
    name="tasks.reconcile_generation_reviews", soft_time_limit=200, time_limit=240, max_retries=0
)
def reconcile_generation_reviews_task():
    from app.services.render_review_admission import sweep_generation_reviews

    return sweep_generation_reviews(budget_s=SWEEP_BUDGET_S)


@celery_app.task(
    name="tasks.prepare_generation_review", soft_time_limit=105, time_limit=115, max_retries=0
)
def prepare_generation_review(review_id: str):
    from app.services.render_review_admission import run_generation_review

    return {"review_id": review_id, "status": run_generation_review(uuid.UUID(review_id))}
