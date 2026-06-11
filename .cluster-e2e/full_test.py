"""Full test video: the earlier local-render job's REAL text-free base
(beat-synced thailand montage + matched song) + cluster intro burned via the
exact production burn step."""
import os
from app.config import settings
from app.storage import _get_client, download_to_file  # type: ignore
from app.pipeline.generative_overlays import build_persistent_intro_overlays
from app.pipeline.text_overlay_skia import burn_text_overlays_skia

JOB = "69d64e04-91d6-4172-b060-7a74f80b56ee"
blobs = [b.name for b in _get_client().bucket(settings.storage_bucket).list_blobs(prefix=f"generative-jobs/{JOB}/")]
print("blobs:", blobs)
base_key = next(n for n in blobs if "base_" in n and "song_text" in n)
local_base = "/work/job_base.mp4"
download_to_file(base_key, local_base)
print("base:", base_key, os.path.getsize(local_base))

for text, tag in [("what's your favorite place?", "favq"), ("the side of thailand nobody films", "thai")]:
    overlays = build_persistent_intro_overlays(
        text=text, effect="fade-in", reveal_window_s=3.0,
        layout="cluster", text_size_px=62, font_family="Playfair Display",
    )
    print(tag, "blocks:", [o["text"] for o in overlays if o["effect"] == "fade-in"])
    out = f"/work/test_video_{tag}.mp4"
    burn_text_overlays_skia(local_base, overlays, out, "/tmp")
    print(" ->", out, os.path.getsize(out))
