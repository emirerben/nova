libvpx 1.17.0, upstream commit 6df3ec34557879fff673706f4a1d9fbd0f3a6f0e.
Source: https://chromium.googlesource.com/webm/libvpx
Only the decoder C source closure is included, with portable generated dispatch headers.
Configuration: --target=generic-gnu --disable-examples --disable-tools --disable-docs --disable-unit-tests --disable-vp8 --disable-vp9-encoder --disable-webm-io --enable-pic
Reproduce headers with configure and make libvpx.a libvpx_srcs.txt. Copy listed C/header files plus generated *_rtcd.h, vpx_config.h and vpx_version.h.
LICENSE, PATENTS and AUTHORS are preserved here. No source changes.
