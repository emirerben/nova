set -eu
mkdir -p /tmp/parity
ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=160x96:rate=30:duration=2 -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/a.mp4 -y
ffmpeg -hide_banner -loglevel error -f lavfi -i testsrc2=size=160x96:rate=30:duration=2 -vf hue=h=120 -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/b.mp4 -y
ffmpeg -hide_banner -loglevel error -i /tmp/parity/a.mp4 -i /tmp/parity/b.mp4 -filter_complex '[0:v][1:v]xfade=transition=fade:duration=1:offset=1' -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/fade.mp4 -y
ffmpeg -hide_banner -loglevel error -i /tmp/parity/a.mp4 -i /tmp/parity/b.mp4 -filter_complex '[0:v][1:v]xfade=transition=fadeblack:duration=1:offset=1' -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/fadeblack.mp4 -y
ffmpeg -hide_banner -loglevel error -i /tmp/parity/a.mp4 -i /tmp/parity/b.mp4 -filter_complex '[0:v][1:v]xfade=transition=fadewhite:duration=1:offset=1' -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/fadewhite.mp4 -y
ffmpeg -hide_banner -loglevel error -i /tmp/parity/a.mp4 -i /tmp/parity/b.mp4 -filter_complex '[0:v][1:v]xfade=transition=wipeleft:duration=1:offset=1' -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/wipeleft.mp4 -y
ffmpeg -hide_banner -loglevel error -i /tmp/parity/a.mp4 -i /tmp/parity/b.mp4 -filter_complex '[0:v][1:v]xfade=transition=wiperight:duration=1:offset=1' -c:v libx264 -preset fast -crf 18 -color_primaries bt709 -color_trc bt709 -colorspace bt709 /tmp/parity/wiperight.mp4 -y
tar -C /tmp/parity -cf - .
