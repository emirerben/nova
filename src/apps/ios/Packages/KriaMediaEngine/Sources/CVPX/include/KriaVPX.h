#ifndef KRIA_VPX_H
#define KRIA_VPX_H
#include <stdint.h>
#include <stddef.h>
typedef struct KriaVPXDecoder KriaVPXDecoder;
KriaVPXDecoder *kria_vpx_create(unsigned int threads);
void kria_vpx_destroy(KriaVPXDecoder *decoder);
int kria_vpx_decode(KriaVPXDecoder *decoder, const uint8_t *bytes, size_t length);
/* Decoder-owned planes remain valid until the next decode/destroy. */
int kria_vpx_frame(KriaVPXDecoder *decoder, unsigned int *width, unsigned int *height,
                   const uint8_t **y, const uint8_t **u, const uint8_t **v,
                   int *y_stride, int *u_stride, int *v_stride, int *full_range);
#endif
