"""Where in the song does an exported excerpt (sampled at 8 kHz f32) actually start? usage: locate.py song.f32 expected_s excerpt.f32..."""
import sys

import numpy as np

SR = 8000
song = np.fromfile(sys.argv[1], dtype=np.float32)
expected = float(sys.argv[2])
for path in sys.argv[3:]:
    clip = np.fromfile(path, dtype=np.float32)[SR : SR + 10 * SR]  # skip the first second
    lo = int((expected - 4) * SR)
    seg = song[lo : lo + 18 * SR]
    n = 1 << int(np.ceil(np.log2(len(clip) + len(seg))))
    cross = np.fft.rfft(seg, n) * np.conj(np.fft.rfft(clip, n))
    cross /= np.abs(cross) + 1e-9
    lag = int(np.argmax(np.fft.irfft(cross, n)[: len(seg)]))
    found = (lo + lag) / SR - 1.0
    print(f"{path.rsplit('/', 1)[-1]}: song position at export t=0 is {found:.3f} s (asked for {expected})")
