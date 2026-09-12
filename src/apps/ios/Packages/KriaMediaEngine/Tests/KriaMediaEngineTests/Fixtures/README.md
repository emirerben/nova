# Synthetic VP9 fixture

`vp9-synthetic.mp4` contains a generated test pattern and sine wave, not uploaded media.

Reproduce with:

```sh
ffmpeg -f lavfi -i testsrc2=size=160x96:rate=12:duration=1 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 -c:v libvpx-vp9 -deadline realtime -cpu-used 8 -b:v 160k -c:a aac -shortest vp9-synthetic.mp4
```
