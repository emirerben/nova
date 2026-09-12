#include "KriaVPX.h"
#include "vpx/vpx_decoder.h"
#include "vpx/vp8dx.h"
#include <stdlib.h>
#include <limits.h>
struct KriaVPXDecoder { vpx_codec_ctx_t codec; vpx_codec_iter_t iterator; };
KriaVPXDecoder *kria_vpx_create(unsigned int threads) {
    KriaVPXDecoder *decoder = calloc(1, sizeof(*decoder));
    if (!decoder) return NULL;
    vpx_codec_dec_cfg_t config = { .threads = threads > 4 ? 4 : threads, .w = 0, .h = 0 };
    if (vpx_codec_dec_init(&decoder->codec, vpx_codec_vp9_dx(), &config, 0) != VPX_CODEC_OK) {
        free(decoder); return NULL;
    }
    return decoder;
}
void kria_vpx_destroy(KriaVPXDecoder *decoder) {
    if (!decoder) return;
    vpx_codec_destroy(&decoder->codec); free(decoder);
}
int kria_vpx_decode(KriaVPXDecoder *decoder, const uint8_t *bytes, size_t length) {
    if (!decoder || !bytes || length > UINT_MAX) return -1;
    decoder->iterator = NULL;
    return vpx_codec_decode(&decoder->codec, bytes, (unsigned int)length, NULL, 0) == VPX_CODEC_OK ? 0 : -1;
}
int kria_vpx_frame(KriaVPXDecoder *decoder, unsigned int *width, unsigned int *height,
                   const uint8_t **y, const uint8_t **u, const uint8_t **v,
                   int *ys, int *us, int *vs, int *full_range) {
    if (!decoder) return -1;
    vpx_image_t *image = vpx_codec_get_frame(&decoder->codec, &decoder->iterator);
    if (!image) return 0;
    if (image->fmt != VPX_IMG_FMT_I420 || image->bit_depth != 8 ||
        !image->d_w || !image->d_h || image->d_w > 8192 || image->d_h > 8192) return -1;
    *width = image->d_w; *height = image->d_h;
    *y = image->planes[0]; *u = image->planes[1]; *v = image->planes[2];
    *ys = image->stride[0]; *us = image->stride[1]; *vs = image->stride[2];
    *full_range = image->range == VPX_CR_FULL_RANGE;
    return 1;
}
