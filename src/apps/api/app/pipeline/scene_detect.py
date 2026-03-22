"""[Stage 3] Scene detection via PySceneDetect."""

from dataclasses import dataclass

import structlog

log = structlog.get_logger()


@dataclass
class SceneCut:
    timestamp_s: float
    score: float


def detect_scenes(video_path: str) -> list[SceneCut]:
    """Detect scene cuts in video_path. Returns list of SceneCut sorted by timestamp."""
    try:
        from scenedetect import AdaptiveDetector, open_video
        from scenedetect.scene_manager import SceneManager
    except ImportError as exc:
        log.warning("scenedetect_not_installed", error=str(exc))
        return []

    log.info("scene_detect_start", path=video_path)

    video = open_video(video_path)
    manager = SceneManager()
    manager.add_detector(AdaptiveDetector())
    manager.detect_scenes(video, show_progress=False)
    scene_list = manager.get_scene_list()

    cuts: list[SceneCut] = []
    for i, (start, _end) in enumerate(scene_list):
        cuts.append(SceneCut(
            timestamp_s=start.get_seconds(),
            score=float(i),  # index as proxy score; detector doesn't expose raw score
        ))

    log.info("scene_detect_done", cut_count=len(cuts))
    return cuts
