"""Motion scenes reference manifest aliases and the shared versioned draw contract."""

from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pipeline.motion_scene import (
    COMPATIBLE_MOTION_RUNTIME_HASHES,
    LEGACY_MOTION_RUNTIME_HASH,
    validate_motion_instances,
)


class MotionSceneProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    instances: list[dict] = Field(min_length=1, max_length=100)
    runtime_hash: str = Field(min_length=1, max_length=200)
    font_asset_id: str = Field(min_length=1, max_length=160)
    image_asset_ids: dict[str, str] = Field(default_factory=dict, max_length=100)

    def validated_instances(self, duration_frames: int | None = None) -> list[dict]:
        scenes = deepcopy(self.instances)
        used = set()
        for scene in scenes:
            params = scene.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("assets"), list):
                continue
            for asset in params["assets"]:
                if not isinstance(asset, dict) or set(asset) != {"asset_id"}:
                    raise ValueError("native motion images must use manifest aliases")
                asset_id = asset.get("asset_id")
                if not isinstance(asset_id, str) or asset_id not in self.image_asset_ids:
                    raise ValueError("native motion image alias is missing")
                used.add(asset_id)
                # Only the storage-form validator sees this inert path. The
                # native renderer reads image_asset_ids and has no storage API.
                asset["gcs_path"] = "users/native/" + asset_id
        if used != set(self.image_asset_ids):
            raise ValueError("motion image aliases must match the scene resources")
        return validate_motion_instances(scenes, duration_frames=duration_frames)

    @model_validator(mode="after")
    def valid_program(self):
        scenes = self.validated_instances()
        allowed = self.runtime_hash in COMPATIBLE_MOTION_RUNTIME_HASHES
        if not allowed and not (
            self.runtime_hash == LEGACY_MOTION_RUNTIME_HASH
            and all(scene["preset_id"] == "route_trace" for scene in scenes)
        ):
            raise ValueError("unsupported motion runtime")
        return self
