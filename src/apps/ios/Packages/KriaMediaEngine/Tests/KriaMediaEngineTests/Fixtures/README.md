# Synthetic VP9 fixture

`vp9-synthetic.mp4` contains a generated test pattern and sine wave, not uploaded media.

Reproduce with:

```sh
ffmpeg -f lavfi -i testsrc2=size=160x96:rate=12:duration=1 -f lavfi -i sine=frequency=440:sample_rate=48000:duration=1 -c:v libvpx-vp9 -deadline realtime -cpu-used 8 -b:v 160k -c:a aac -shortest vp9-synthetic.mp4
```

# Real talking-head frame

`speaker-matte-talking.jpg` is one frame of the app's own poster clip
`src/apps/web/public/plan/type-posters/talking.mp4` (416x740, a person against a plain wall). It feeds the
real-Vision speaker-matte test (`SpeakerMatteTests.testRealVisionCutsOutARealSpeaker`).

Reproduce with:

```sh
ffmpeg -ss 2.5 -i src/apps/web/public/plan/type-posters/talking.mp4 -frames:v 1 -q:v 4 speaker-matte-talking.jpg
```
