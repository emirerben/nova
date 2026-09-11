"""Additional production pilot limits, separate from renderer implementation."""

from app.config import settings
from app.kria.recipes_v2 import EditRecipeV2


def validate_phone_pilot_recipe(recipe: EditRecipeV2) -> None:
    if not recipe.required_capabilities.issubset(settings.phone_render_verified_features):
        raise ValueError("This edit needs a phone capability that is not enabled")
    if any(
        layer.giant_title is not None and layer.effect == "handwriting"
        for layer in recipe.text_layers
    ):
        raise ValueError(
            "Giant-title handwriting is unavailable while phone performance is improved"
        )
