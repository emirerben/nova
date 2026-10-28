"""Digital silence in place of the voice guard's audio decode (KRI-1017).

``analyze_speech_cleanup`` and the in-render cleanup path now decode their media so
``speech_voice_guard`` can veto cuts that hold a voice. Tests that feed those paths a
placeholder path (``/dev/null``, ``take.wav``) or a stubbed ``subprocess`` have no audio to
decode, and an undecodable file drops every judged cut by design. Digital silence holds no
voiced run, so the guard keeps every cut and those tests keep judging the planner, not the
guard. The guard itself is exercised on real samples in
``tests/pipeline/test_voice_guard_cleanup_e2e.py`` and ``test_kri1017_maximo2_replay.py``.
"""

from __future__ import annotations

import numpy as np


def silent_pcm(_path: str, *_args: object, **_kwargs: object) -> np.ndarray:
    return np.zeros(16_000, dtype=np.float32)
