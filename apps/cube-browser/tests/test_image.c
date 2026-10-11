#include "image.h"

#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <jpeglib.h>
#include <png.h>
#include <stdlib.h>
#include <string.h>

static unsigned char *tiny_png(size_t *length)
{
    const unsigned char rgba[] = {255, 0, 0, 255, 0, 255, 128, 128};
    png_image image = {.version = PNG_IMAGE_VERSION, .width = 2, .height = 1,
                       .format = PNG_FORMAT_RGBA};
    png_alloc_size_t size = 0;
    assert(png_image_write_to_memory(&image, NULL, &size, 0, rgba, 0, NULL));
    unsigned char *encoded = malloc(size);
    assert(encoded);
    assert(png_image_write_to_memory(&image, encoded, &size, 0, rgba, 0, NULL));
    *length = size;
    return encoded;
}

static unsigned char *tiny_jpeg(size_t *length, bool progressive, bool gray)
{
    struct jpeg_compress_struct encoder;
    struct jpeg_error_mgr error;
    encoder.err = jpeg_std_error(&error);
    jpeg_create_compress(&encoder);
    unsigned char *encoded = NULL;
    unsigned long size = 0;
    jpeg_mem_dest(&encoder, &encoded, &size);
    encoder.image_width = 2;
    encoder.image_height = 2;
    encoder.input_components = gray ? 1 : 3;
    encoder.in_color_space = gray ? JCS_GRAYSCALE : JCS_RGB;
    jpeg_set_defaults(&encoder);
    jpeg_set_quality(&encoder, 100, TRUE);
    if (progressive) jpeg_simple_progression(&encoder);
    jpeg_start_compress(&encoder, TRUE);
    unsigned char rgb[] = {255, 0, 0, 255, 0, 0};
    unsigned char grayscale[] = {200, 200};
    JSAMPROW rows[] = {gray ? grayscale : rgb};
    while (encoder.next_scanline < encoder.image_height)
        assert(jpeg_write_scanlines(&encoder, rows, 1) == 1);
    jpeg_finish_compress(&encoder);
    jpeg_destroy_compress(&encoder);
    *length = size;
    return encoded;
}

static uint32_t pixel(cairo_surface_t *surface, unsigned x, unsigned y)
{
    cairo_surface_flush(surface);
    unsigned char *data = cairo_image_surface_get_data(surface);
    int stride = cairo_image_surface_get_stride(surface);
    uint32_t color;
    memcpy(&color, data + (size_t)y * (size_t)stride + (size_t)x * 4, sizeof(color));
    return color;
}

static void rejected(const void *data, size_t length, const char *mime)
{
    char error[96] = {0};
    cairo_surface_t *surface = cb_image_decode(data, length, mime, error, sizeof(error));
    assert(!surface);
    assert(error[0] && error[sizeof(error) - 1] == '\0');
}

static void put_u32(unsigned char *data, uint32_t value)
{
    data[0] = (unsigned char)(value >> 24);
    data[1] = (unsigned char)(value >> 16);
    data[2] = (unsigned char)(value >> 8);
    data[3] = (unsigned char)value;
}

static uint32_t crc32_bytes(const unsigned char *data, size_t size)
{
    uint32_t crc = UINT32_MAX;
    while (size--) {
        crc ^= *data++;
        for (int bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (UINT32_C(0xedb88320) & (uint32_t)-(int32_t)(crc & 1));
    }
    return ~crc;
}

static void png_size(unsigned char *encoded, uint32_t width, uint32_t height)
{
    put_u32(encoded + 16, width);
    put_u32(encoded + 20, height);
    put_u32(encoded + 29, crc32_bytes(encoded + 12, 17));
}

static void jpeg_size(unsigned char *encoded, size_t length, uint16_t width, uint16_t height)
{
    for (size_t i = 2; i + 8 < length; i++) {
        if (encoded[i] == 0xff && (encoded[i + 1] == 0xc0 || encoded[i + 1] == 0xc2)) {
            encoded[i + 5] = (unsigned char)(height >> 8);
            encoded[i + 6] = (unsigned char)height;
            encoded[i + 7] = (unsigned char)(width >> 8);
            encoded[i + 8] = (unsigned char)width;
            return;
        }
    }
    assert(!"JPEG fixture lacks a SOF marker");
}

static void png_tests(void)
{
    size_t length;
    unsigned char *encoded = tiny_png(&length);
    char error[128];
    cairo_surface_t *surface = cb_image_decode(encoded, length, "image/png", error, sizeof(error));
    assert(surface && !error[0]);
    assert(cairo_image_surface_get_width(surface) == 2);
    assert(cairo_image_surface_get_height(surface) == 1);
    assert(cairo_image_surface_get_format(surface) == CAIRO_FORMAT_ARGB32);
    assert(pixel(surface, 0, 0) == UINT32_C(0xffff0000));
    assert(pixel(surface, 1, 0) == UINT32_C(0x80008040));
    cairo_surface_destroy(surface);
    rejected(encoded, length, "image/jpeg");
    rejected(encoded, length, "image/svg+xml");
    rejected(encoded, length, NULL);
    rejected(encoded, CB_IMAGE_MAX_BYTES + 1, "image/png");
    rejected(encoded, 8, "image/png");
    rejected(encoded, length - 1, "image/png");
    rejected(encoded, length - 12, "image/png");
    for (size_t cut = 0; cut < length; cut++) rejected(encoded, cut, "image/png");
    unsigned char *mutated = malloc(length + 1);
    assert(mutated);
    memcpy(mutated, encoded, length);
    mutated[length] = 0;
    rejected(mutated, length + 1, "image/png");
    png_size(mutated, 4097, 1);
    rejected(mutated, length, "image/png");
    png_size(mutated, 4096, 2000);
    rejected(mutated, length, "image/png");
    png_size(mutated, 0, 1);
    rejected(mutated, length, "image/png");
    memcpy(mutated, encoded, length);
    mutated[29] ^= 1; /* Broken IHDR CRC is never repaired. */
    rejected(mutated, length, "image/png");
    memcpy(mutated, encoded, length);
    mutated[length - 1] ^= 1; /* The end marker CRC is checked too. */
    rejected(mutated, length, "image/png");
    free(mutated);
    free(encoded);
}

static void jpeg_tests(void)
{
    for (unsigned variant = 0; variant < 3; variant++) {
        size_t length;
        unsigned char *encoded = tiny_jpeg(&length, variant == 1, variant == 2);
        char error[128];
        cairo_surface_t *surface = cb_image_decode(encoded, length, "image/jpeg", error, sizeof(error));
        assert(surface && !error[0]);
        assert(cairo_image_surface_get_width(surface) == 2);
        assert(cairo_image_surface_get_height(surface) == 2);
        assert(cairo_image_surface_get_format(surface) == CAIRO_FORMAT_RGB24);
        uint32_t color = pixel(surface, 1, 1);
        if (variant == 2) {
            assert(((color >> 16) & 255) >= 198 && ((color >> 16) & 255) <= 202);
            assert(((color >> 8) & 255) == (color & 255));
        } else {
            assert(((color >> 16) & 255) >= 250);
            assert(((color >> 8) & 255) <= 3 && (color & 255) <= 3);
        }
        cairo_surface_destroy(surface);
        rejected(encoded, length, "image/png");
        rejected(encoded, length - 2, "image/jpeg");
        rejected(encoded, length / 2, "image/jpeg");
        for (size_t cut = 0; cut < length; cut++) rejected(encoded, cut, "image/jpeg");
        jpeg_size(encoded, length, 4097, 1);
        rejected(encoded, length, "image/jpeg");
        jpeg_size(encoded, length, 4096, 2000);
        rejected(encoded, length, "image/jpeg");
        free(encoded);
    }
}

static cairo_surface_t *svg(const char *xml)
{
    char error[256];
    cairo_surface_t *surface=cb_image_decode(xml,strlen(xml),"image/svg+xml",error,sizeof error);
    if (!surface) fprintf(stderr,"SVG decode failed: %s\n%s\n",error,xml);
    assert(surface && !error[0]);
    assert(cairo_image_surface_get_format(surface)==CAIRO_FORMAT_ARGB32);
    return surface;
}

static void svg_pixels(void)
{
    const char *basic="<svg xmlns='http://www.w3.org/2000/svg' width='8' height='8'>"
        "<rect x='2' y='2' width='4' height='4' fill='#ff0000' fill-opacity='0.5'/></svg>";
    cairo_surface_t *surface=svg(basic);
    assert(cairo_image_surface_get_width(surface)==8 && cairo_image_surface_get_height(surface)==8);
    assert(pixel(surface,0,0)==0);
    uint32_t half=pixel(surface,3,3);
    assert((half>>24)>=127 && (half>>24)<=128 && ((half>>16)&255)==(half>>24) && !(half&65535));
    cairo_surface_destroy(surface);
    for (size_t cut=0;cut<strlen(basic);cut++) rejected(basic,cut,"image/svg+xml");
    rejected(basic,strlen(basic),"image/png");
    rejected(basic,strlen(basic),"image/jpeg");
    rejected(basic,strlen(basic),"IMAGE/SVG+XML"); /* Engine canonicalizes MIME. */

    surface=svg("<svg width='20' height='20' viewBox='0 0 10 20'><rect width='10' height='20' fill='red'/></svg>");
    assert(pixel(surface,1,10)==0 && pixel(surface,10,10)==UINT32_C(0xffff0000));
    cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='20' viewBox='0 0 10 20' preserveAspectRatio='none'>"
        "<rect width='10' height='20' fill='red'/></svg>");
    assert(pixel(surface,1,10)==UINT32_C(0xffff0000)); cairo_surface_destroy(surface);
    surface=svg("<svg viewBox='0 0 12 8'><g transform='translate(2 1) scale(2)' fill='blue'>"
        "<path d='M0 0h4v2H0z'/></g></svg>");
    assert(cairo_image_surface_get_width(surface)==12 && cairo_image_surface_get_height(surface)==8);
    assert(pixel(surface,3,2)==UINT32_C(0xff0000ff) && pixel(surface,0,0)==0); cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='20'><path d='M2 10 C2 2 18 2 18 10 Q10 18 2 10Z' fill='#00ff00'/></svg>");
    assert(pixel(surface,10,8)==UINT32_C(0xff00ff00)); cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='20'><path d='M2 10 A8 8 0 1 1 18 10 A8 8 0 1 1 2 10Z' fill='red'/></svg>");
    assert(pixel(surface,10,10)==UINT32_C(0xffff0000)); cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='20'><circle cx='5' cy='5' r='4' fill='red'/>"
        "<ellipse cx='15' cy='5' rx='4' ry='3' fill='blue'/>"
        "<polygon points='1,11 9,11 9,19 1,19' fill='lime'/>"
        "<polyline points='11,15 15,11 19,15' fill='none' stroke='black' stroke-width='2'/>"
        "<line x1='11' y1='18' x2='19' y2='18' stroke='red' stroke-width='2'/></svg>");
    assert(pixel(surface,5,5)==UINT32_C(0xffff0000));
    assert(pixel(surface,15,5)==UINT32_C(0xff0000ff));
    assert(pixel(surface,5,15)==UINT32_C(0xff00ff00));
    assert(pixel(surface,15,18)==UINT32_C(0xffff0000)); cairo_surface_destroy(surface);
    surface=svg("<?xml version='1.0' encoding='UTF-8'?><svg width='4' height='4'>"
        "<!-- <rect width='4' height='4' fill='red'/> -->"
        "<title>A &amp; B</title><desc>&lt;script&gt;no code&lt;/script&gt;</desc>"
        "<rect width='4' height='4' fill='red' style='fill:rgb(0, 255, 0);fill-opacity:1'/></svg>");
    assert(pixel(surface,1,1)==UINT32_C(0xff00ff00)); cairo_surface_destroy(surface);
    surface=svg("<svg width='4' height='4'><rect width='4' height='4' fill='&#35;00ff00'/></svg>");
    assert(pixel(surface,1,1)==UINT32_C(0xff00ff00)); cairo_surface_destroy(surface);
    surface=svg("<svg width=' 8 ' height=' 4 ' viewBox=' 0 0 8 4 '><rect width=' 8 ' height=' 4 ' fill='red'/></svg>");
    assert(cairo_image_surface_get_width(surface)==8 && cairo_image_surface_get_height(surface)==4);
    assert(pixel(surface,7,3)==UINT32_C(0xffff0000)); cairo_surface_destroy(surface);
    surface=svg("<svg width='12' height='12'><path d='M1 1H11V11H1Z M4 4H8V8H4Z' fill='red' fill-rule='evenodd'/></svg>");
    assert(pixel(surface,2,2)==UINT32_C(0xffff0000) && !pixel(surface,6,6)); cairo_surface_destroy(surface);
    surface=svg("<svg width='12' height='12'><rect x='2' y='2' width='8' height='8' fill='red' stroke='blue' stroke-width='4' opacity='.5'/></svg>");
    assert((pixel(surface,3,3)>>24)==128 && (pixel(surface,6,6)>>24)==128); cairo_surface_destroy(surface);
}

static void svg_gradients(void)
{
    cairo_surface_t *surface=svg("<svg width='20' height='4'><defs><linearGradient id='color'>"
        "<stop offset='0' stop-color='red'/><stop offset='100%' style='stop-color:blue'/></linearGradient></defs>"
        "<rect width='20' height='4' fill='url(#color)'/></svg>");
    uint32_t left=pixel(surface,1,1),right=pixel(surface,18,1);
    assert(((left>>16)&255)>220 && (left&255)<35);
    assert((right&255)>220 && ((right>>16)&255)<35); cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='20'><rect width='20' height='20' fill='url(#radial)'/>"
        "<defs><radialGradient id='radial'><stop offset='0' stop-color='white'/>"
        "<stop offset='1' stop-color='black'/></radialGradient></defs></svg>");
    assert((pixel(surface,10,10)&255)>225 && (pixel(surface,0,0)&255)<5); cairo_surface_destroy(surface);
    surface=svg("<svg width='20' height='4'><defs><linearGradient id='color' gradientUnits='userSpaceOnUse' x1='0' y1='0' x2='10' y2='0' spreadMethod='reflect'>"
        "<stop offset='0' stop-color='red'/><stop offset='1' stop-color='blue'/></linearGradient></defs>"
        "<rect width='20' height='4' fill='url(#color)'/></svg>");
    left=pixel(surface,1,1);right=pixel(surface,18,1);
    assert(abs((int)((left>>16)&255)-(int)((right>>16)&255))<3); cairo_surface_destroy(surface);
}

static void svg_rejections(void)
{
    const char *bad[]={
        "<svg>","<svg width='2' height='2'><rect></svg>","<svg/><svg/>","<svg width=2 height='2'/>",
        "<svg width='2' width='3' height='2'/>","<svg xmlns='https://wrong.example/' width='2' height='2'/>",
        "<svg width='2' height='2'>&unknown;</svg>","<svg width='2' height='2'><!-- unterminated</svg>",
        "<!DOCTYPE svg SYSTEM 'file:///etc/passwd'><svg width='2' height='2'/>",
        "<!DOCTYPE svg [<!ENTITY x SYSTEM 'http://127.0.0.1/secret'>]><svg width='2' height='2'>&x;</svg>",
        "<?xml-stylesheet href='https://example.org/x.css'?><svg width='2' height='2'/>",
        "<svg width='2' height='2'><image href='file:///etc/passwd'/></svg>",
        "<svg width='2' height='2'><image href='http://127.0.0.1/image.png'/></svg>",
        "<svg width='2' height='2'><script>alert(1)</script></svg>",
        "<svg width='2' height='2'><text x='0' y='1'>Text</text></svg>",
        "<svg width='2' height='2'><foreignObject/></svg>",
        "<svg width='2' height='2'><style>rect{fill:red}</style></svg>",
        "<svg width='2' height='2'><use href='#thing'/></svg>",
        "<svg width='2' height='2' onload='alert(1)'/>",
        "<svg width='2' height='2'><rect width='2' height='2' fill='url(http://127.0.0.1/paint)'/></svg>",
        "<svg width='2' height='2'><rect width='2' height='2' fill='url(#missing)'/></svg>",
        "<svg width='2' height='2'><rect width='2' height='2' filter='url(#f)'/></svg>",
        "<svg width='2' height='2'><rect width='2' height='2' style='clip-path:url(#c)'/></svg>",
        "<svg width='2' height='2'><g opacity='.5'><rect width='2' height='2'/></g></svg>",
        "<svg width='2' height='2'><defs><linearGradient id='g'/></defs></svg>",
        "<svg width='2' height='2'><defs><linearGradient id='g' href='https://example.org/'/></defs></svg>",
        "<svg width='2' height='2'><rect id='same'/><rect id='same'/></svg>",
        "<svg width='2' height='2'><rect fill='currentColor'/></svg>",
        "<svg width='2' height='2'><rect fill='#ff000080'/></svg>",
        "<svg width='2' height='2'><path d='M0 0L1'/></svg>",
        "<svg width='2' height='2'><path d='M0 0 A1 1 0 5 1 1 1'/></svg>",
        "<svg width='2' height='2'><path d='M0 0 A.00001 1 0 0 1 1 1'/></svg>",
        "<svg width='2' height='2'><rect transform='matrix(1 2 3)'/></svg>",
        "<svg width='2' height='2'><rect transform='scale(0)'/></svg>",
        "<svg width='2' height='2'><rect transform='translate(1e309)'/></svg>",
        "<svg width='4097' height='1'/>","<svg width='4096' height='2000'/>",
        "<svg width='0' height='2'/>","<svg width='-2' height='2'/>",
        "<svg width='2' height='NaN'/>","<svg width='2' height='inf'/>",
        "<svg width='100%' height='2'/>","<svg viewBox='0 0 0 2'/>",
        "<svg viewBox='0 0 .0000001 2'/>",
        "<svg width='2' height='2'><rect x='9999999999999999999999999'/></svg>",
        "<svg width='2' height='2'><rect stroke-dasharray='.00001 .00001'/></svg>",
        NULL
    };
    for (size_t i=0;bad[i];i++) rejected(bad[i],strlen(bad[i]),"image/svg+xml");
    const char embedded[]="<svg width='2' height='2'>\0</svg>";
    rejected(embedded,sizeof embedded-1,"image/svg+xml");
    rejected("\xff<svg/>",7,"image/svg+xml");
    char *complex=malloc(500000);assert(complex);
    strcpy(complex,"<svg width='2' height='2'>");
    for (unsigned i=0;i<40;i++) strcat(complex,"<g>");
    for (unsigned i=0;i<40;i++) strcat(complex,"</g>");
    strcat(complex,"</svg>");rejected(complex,strlen(complex),"image/svg+xml");
    strcpy(complex,"<svg width='2' height='2'>");
    for (unsigned i=0;i<513;i++) strcat(complex,"<rect width='2' height='2'/>");
    strcat(complex,"</svg>");rejected(complex,strlen(complex),"image/svg+xml");
    strcpy(complex,"<svg width='2' height='2'><path d='M0 0");
    for (unsigned i=0;i<17000;i++) strcat(complex,"L1 1");
    strcat(complex,"'/></svg>");rejected(complex,strlen(complex),"image/svg+xml");
    strcpy(complex,"<svg width='2' height='2'><defs><linearGradient id='g'>");
    for (unsigned i=0;i<65;i++) strcat(complex,"<stop offset='0' stop-color='red'/>");
    strcat(complex,"</linearGradient></defs></svg>");rejected(complex,strlen(complex),"image/svg+xml");
    strcpy(complex,"<svg width='4000' height='2000'>");
    for (unsigned i=0;i<5;i++) strcat(complex,"<rect width='4000' height='2000'/>");
    strcat(complex,"</svg>");rejected(complex,strlen(complex),"image/svg+xml");
    strcpy(complex,"<svg width='2' height='2'><rect width='2' height='2' style='");
    for (unsigned i=0;i<127;i++) strcat(complex,"fill:red;");
    strcat(complex,"fill:blue'/></svg>");rejected(complex,strlen(complex),"image/svg+xml");
    free(complex);
}

int main(void)
{
    rejected(NULL, 0, "image/png");
    rejected("not an image", 12, "image/png");
    rejected("not an image", 12, "image/jpeg");
    rejected("GIF89a", 6, "image/gif");
    png_tests();
    jpeg_tests();
    svg_pixels();
    svg_gradients();
    svg_rejections();
    puts("images: PNG/JPEG, static SVG paths/gradients/alpha, MIME, XML, forbidden features and budgets passed");
    return 0;
}
