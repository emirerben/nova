"""Generate tiny decoded xfade samples; no raw media is retained in the repo.

Run with Python + FFmpeg, or inside the production render image. Redirect stdout
to tests/fixtures/phone_clip_transitions.json when deliberately refreshing it.
"""

import json
import subprocess


def frames(effect: str, pixel_format: str) -> bytes:
    return subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=0x808080:s=16x16:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "color=white:s=16x16:r=10:d=2",
            "-filter_complex",
            f"[0:v][1:v]xfade=transition={effect}:duration=1:offset=1,format={pixel_format}",
            "-f",
            "rawvideo",
            "-",
        ]
    )


def main() -> None:
    rows = []
    for effect in ["fadeblack", "fadewhite", "wipeleft", "wiperight"]:
        luma = frames(effect, "yuv444p")
        rgb = frames(effect, "rgb24")
        for index in range(10, 20):
            start = index * 768
            rows.append(
                {
                    "effect": effect,
                    "progress": (index - 10) / 10,
                    "luma_row": list(luma[start + 128 : start + 144]),
                    "rgb_row": list(rgb[start + 384 : start + 432]),
                }
            )
    version = subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0]
    print(
        json.dumps(
            {
                "generator": version + "; gray128 to white, 16x16, 10fps, duration=1 offset=1",
                "outgoing_luma": 126,
                "incoming_luma": 235,
                "rows": rows,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
