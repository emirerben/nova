set -eu
mkdir -p /tmp/look-parity
# Deterministic 512-square YUV420 input covers every luma/chroma pair. Each
# cell also has different neighboring luma to detect wrong chroma sampling.
python - <<'PY'
from pathlib import Path
w = h = 512
y = bytearray(w*h)
u = bytearray(w*h//4)
v = bytearray(w*h//4)
for row in range(256):
    for col in range(256):
        for dy in range(2):
            for dx in range(2):
                y[(row*2+dy)*w+col*2+dx] = (row+dx*37+dy*71)%256
        u[row*256+col] = col
        v[row*256+col] = 255-col
Path('/tmp/look-parity/input.yuv').write_bytes(y+u+v)
PY
LOOK=$(python - <<'PY'
from app.pipeline.look_presets import golden_hour_filter
print(golden_hour_filter(width=512, height=512))
PY
)
ffmpeg -hide_banner -loglevel error -f rawvideo -pixel_format yuv420p -video_size 512x512 -i /tmp/look-parity/input.yuv -vf "$LOOK" -frames:v 1 -f rawvideo /tmp/look-parity/golden.yuv -y
tar -C /tmp/look-parity -cf - .
