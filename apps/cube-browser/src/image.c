#include "image.h"
#include "svg.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <jpeglib.h>
#include <png.h>
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>

static void image_error(char *error, size_t size, const char *message)
{
    if (error && size) snprintf(error, size, "%s", message);
}

static bool valid_size(uint32_t width, uint32_t height)
{
    return width && height && width <= CB_IMAGE_MAX_DIMENSION &&
        height <= CB_IMAGE_MAX_DIMENSION && (uint64_t)width * height <= CB_IMAGE_MAX_PIXELS;
}

static uint32_t big_endian_u32(const unsigned char *data)
{
    return (uint32_t)data[0] << 24 | (uint32_t)data[1] << 16 |
           (uint32_t)data[2] << 8 | data[3];
}

static uint32_t png_chunk_crc(const unsigned char *data, size_t length)
{
    uint32_t crc = UINT32_MAX;
    while (length--) {
        crc ^= *data++;
        for (unsigned bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (uint32_t)-(int32_t)(crc & 1));
    }
    return ~crc;
}

static bool png_envelope(const unsigned char *data, size_t length,
                         char *error, size_t error_size)
{
    if (length < 33 || png_sig_cmp(data, 0, 8) || big_endian_u32(data + 8) != 13 ||
        memcmp(data + 12, "IHDR", 4)) {
        image_error(error, error_size, "Malformed PNG header");
        return false;
    }
    if (!valid_size(big_endian_u32(data + 16), big_endian_u32(data + 20))) {
        image_error(error, error_size, "Image dimensions exceed the decoding budget");
        return false;
    }
    bool pixels = false, after_pixels = false, palette = false;
    size_t offset = 8;
    while (length - offset >= 12) {
        uint32_t size = big_endian_u32(data + offset);
        if (size > length - offset - 12) break;
        const unsigned char *type = data + offset + 4;
        for (unsigned i = 0; i < 4; i++) {
            if ((type[i] < 'A' || type[i] > 'Z') && (type[i] < 'a' || type[i] > 'z')) {
                image_error(error, error_size, "Invalid PNG chunk type");
                return false;
            }
        }
        if ((type[2] & 32) || png_chunk_crc(type, (size_t)size + 4) != big_endian_u32(type + 4 + size)) {
            image_error(error, error_size, "Invalid PNG chunk checksum or reserved bit");
            return false;
        }
        if (!memcmp(type, "acTL", 4) || !memcmp(type, "fcTL", 4) || !memcmp(type, "fdAT", 4)) {
            image_error(error, error_size, "Animated PNG is not supported");
            return false;
        }
        if (!memcmp(type, "IHDR", 4)) {
            if (offset != 8) break;
        } else if (!memcmp(type, "PLTE", 4)) {
            if (pixels || palette || !size || size > 768 || size % 3) break;
            palette = true;
        } else if (!memcmp(type, "IDAT", 4)) {
            if (after_pixels) break;
            pixels = true;
        } else if (memcmp(type, "IEND", 4) && !(type[0] & 32)) {
            image_error(error, error_size, "Unsupported critical PNG chunk");
            return false;
        }
        if (pixels && memcmp(type, "IDAT", 4)) after_pixels = true;
        offset += (size_t)size + 12;
        if (!memcmp(type, "IEND", 4)) {
            if (!size && pixels && offset == length) return true;
            break;
        }
    }
    image_error(error, error_size, "Truncated or malformed PNG chunks");
    return false;
}

static cairo_surface_t *decode_png(const unsigned char *data, size_t length,
                                    char *error, size_t error_size)
{
    if (!png_envelope(data, length, error, error_size)) return NULL;
    png_image image = {.version = PNG_IMAGE_VERSION};
    if (!png_image_begin_read_from_memory(&image, data, length) || image.warning_or_error) {
        image_error(error, error_size, "Malformed PNG data");
        png_image_free(&image);
        return NULL;
    }
    if (!valid_size(image.width, image.height)) {
        image_error(error, error_size, "Image dimensions exceed the decoding budget");
        png_image_free(&image);
        return NULL;
    }
    image.format = PNG_FORMAT_RGBA;
    cairo_surface_t *surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32,
                                                         (int)image.width, (int)image.height);
    if (cairo_surface_status(surface) != CAIRO_STATUS_SUCCESS) {
        image_error(error, error_size, "Cannot allocate the image surface");
        png_image_free(&image);
        cairo_surface_destroy(surface);
        return NULL;
    }
    unsigned char *pixels = cairo_image_surface_get_data(surface);
    int stride = cairo_image_surface_get_stride(surface);
    if (!png_image_finish_read(&image, NULL, pixels, stride, NULL) || image.warning_or_error) {
        image_error(error, error_size, "Truncated or malformed PNG pixels");
        png_image_free(&image);
        cairo_surface_destroy(surface);
        return NULL;
    }
    for (uint32_t y = 0; y < image.height; y++) {
        unsigned char *row = pixels + (size_t)y * (size_t)stride;
        for (uint32_t x = 0; x < image.width; x++) {
            unsigned char *rgba = row + (size_t)x * 4;
            uint32_t alpha = rgba[3];
            uint32_t red = (rgba[0] * alpha + 127) / 255;
            uint32_t green = (rgba[1] * alpha + 127) / 255;
            uint32_t blue = (rgba[2] * alpha + 127) / 255;
            uint32_t argb = alpha << 24 | red << 16 | green << 8 | blue;
            memcpy(rgba, &argb, sizeof(argb));
        }
    }
    png_image_free(&image);
    cairo_surface_mark_dirty(surface);
    return surface;
}

struct jpeg_error {
    struct jpeg_error_mgr manager;
    jmp_buf jump;
};

struct jpeg_context {
    struct jpeg_decompress_struct codec;
    struct jpeg_error error;
    struct jpeg_progress_mgr progress;
    cairo_surface_t *surface;
    unsigned char *row;
};

static void jpeg_failure(j_common_ptr codec)
{
    struct jpeg_error *error = (struct jpeg_error *)codec->err;
    longjmp(error->jump, 1);
}

static void jpeg_message(j_common_ptr codec, int level)
{
    /* libjpeg normally repairs truncation by inserting EOI; reject warnings. */
    if (level < 0) jpeg_failure(codec);
}

static void jpeg_progress(j_common_ptr codec)
{
    struct jpeg_decompress_struct *decoder = (struct jpeg_decompress_struct *)codec;
    /* Bound pathological progressive streams as well as decoded pixels. */
    if (decoder->input_scan_number > 100) jpeg_failure(codec);
}

static void jpeg_context_free(struct jpeg_context *context)
{
    jpeg_destroy_decompress(&context->codec);
    if (context->surface) cairo_surface_destroy(context->surface);
    free(context->row);
    free(context);
}

static cairo_surface_t *decode_jpeg(const unsigned char *data, size_t length,
                                     char *error, size_t error_size)
{
    if (length < 4 || data[0] != 0xff || data[1] != 0xd8 || data[2] != 0xff) {
        image_error(error, error_size, "MIME type does not match a JPEG signature");
        return NULL;
    }
    /* Mutable state lives on the heap so longjmp cannot invalidate locals. */
    struct jpeg_context *context = calloc(1, sizeof(*context));
    if (!context) {
        image_error(error, error_size, "Cannot allocate the JPEG decoder");
        return NULL;
    }
    context->codec.err = jpeg_std_error(&context->error.manager);
    context->error.manager.error_exit = jpeg_failure;
    context->error.manager.emit_message = jpeg_message;
    if (setjmp(context->error.jump)) {
        image_error(error, error_size, "Truncated, malformed or unsupported JPEG data");
        jpeg_context_free(context);
        return NULL;
    }
    jpeg_create_decompress(&context->codec);
    context->codec.mem->max_memory_to_use = 64L * 1024L * 1024L;
    context->progress.progress_monitor = jpeg_progress;
    context->codec.progress = &context->progress;
    jpeg_mem_src(&context->codec, data, (unsigned long)length);
    if (jpeg_read_header(&context->codec, TRUE) != JPEG_HEADER_OK ||
        !valid_size(context->codec.image_width, context->codec.image_height)) {
        image_error(error, error_size, "Image dimensions exceed the decoding budget");
        jpeg_context_free(context);
        return NULL;
    }
    if (context->codec.data_precision != 8 ||
        (context->codec.jpeg_color_space != JCS_GRAYSCALE &&
         context->codec.jpeg_color_space != JCS_RGB &&
         context->codec.jpeg_color_space != JCS_YCbCr)) {
        image_error(error, error_size, "Unsupported JPEG color space or precision");
        jpeg_context_free(context);
        return NULL;
    }
    context->codec.out_color_space = JCS_RGB;
    if (!jpeg_start_decompress(&context->codec) || context->codec.output_components != 3 ||
        !valid_size(context->codec.output_width, context->codec.output_height)) {
        image_error(error, error_size, "Invalid JPEG output dimensions");
        jpeg_context_free(context);
        return NULL;
    }
    context->surface = cairo_image_surface_create(CAIRO_FORMAT_RGB24,
        (int)context->codec.output_width, (int)context->codec.output_height);
    context->row = malloc((size_t)context->codec.output_width * 3);
    if (cairo_surface_status(context->surface) != CAIRO_STATUS_SUCCESS || !context->row) {
        image_error(error, error_size, "Cannot allocate the image surface");
        jpeg_context_free(context);
        return NULL;
    }
    unsigned char *pixels = cairo_image_surface_get_data(context->surface);
    int stride = cairo_image_surface_get_stride(context->surface);
    while (context->codec.output_scanline < context->codec.output_height) {
        size_t y = context->codec.output_scanline;
        JSAMPROW rows[] = {context->row};
        if (jpeg_read_scanlines(&context->codec, rows, 1) != 1) jpeg_failure((j_common_ptr)&context->codec);
        for (uint32_t x = 0; x < context->codec.output_width; x++) {
            unsigned char *rgb = context->row + (size_t)x * 3;
            uint32_t color = UINT32_C(0xff000000) | (uint32_t)rgb[0] << 16 |
                             (uint32_t)rgb[1] << 8 | rgb[2];
            memcpy(pixels + y * (size_t)stride + (size_t)x * 4, &color, sizeof(color));
        }
    }
    if (!jpeg_finish_decompress(&context->codec)) jpeg_failure((j_common_ptr)&context->codec);
    cairo_surface_t *surface = context->surface;
    context->surface = NULL;
    jpeg_context_free(context);
    cairo_surface_mark_dirty(surface);
    return surface;
}

cairo_surface_t *cb_image_decode(const void *data, size_t length, const char *mime,
                                  char *error, size_t error_size)
{
    if (error && error_size) error[0] = '\0';
    if (!data || !length || length > CB_IMAGE_MAX_BYTES) {
        image_error(error, error_size, "Encoded image is empty or exceeds the 2 MiB budget");
        return NULL;
    }
    if (mime && !strcmp(mime, "image/png")) return decode_png(data, length, error, error_size);
    if (mime && !strcmp(mime, "image/jpeg")) return decode_jpeg(data, length, error, error_size);
    if (mime && !strcmp(mime, "image/svg+xml")) return cb_svg_decode(data, length, error, error_size);
    image_error(error, error_size, "Only image/png, image/jpeg and static image/svg+xml are supported");
    return NULL;
}
