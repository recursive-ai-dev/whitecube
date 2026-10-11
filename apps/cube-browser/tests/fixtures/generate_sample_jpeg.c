/* Recreate sample.jpg from synthetic RGB pixels; no external image input.
 * cc -I.deps/prefix/include tests/fixtures/generate_sample_jpeg.c \
 *    .deps/prefix/lib/libjpeg.a -o .deps/generate-sample-jpeg
 * .deps/generate-sample-jpeg tests/fixtures/sample.jpg
 */
#include <stdio.h>
#include <jpeglib.h>

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    FILE *output = fopen(argv[1], "wb");
    if (!output) return 1;
    struct jpeg_compress_struct encoder;
    struct jpeg_error_mgr error;
    encoder.err = jpeg_std_error(&error);
    jpeg_create_compress(&encoder);
    jpeg_stdio_dest(&encoder, output);
    encoder.image_width = 8;
    encoder.image_height = 4;
    encoder.input_components = 3;
    encoder.in_color_space = JCS_RGB;
    jpeg_set_defaults(&encoder);
    jpeg_set_quality(&encoder, 100, TRUE);
    for (int i = 0; i < encoder.num_components; i++) {
        encoder.comp_info[i].h_samp_factor = 1;
        encoder.comp_info[i].v_samp_factor = 1;
    }
    jpeg_start_compress(&encoder, TRUE);
    unsigned char red[8 * 3];
    for (unsigned i = 0; i < sizeof(red); i++) red[i] = i % 3 ? 0 : 255;
    JSAMPROW rows[] = {red};
    while (encoder.next_scanline < encoder.image_height)
        jpeg_write_scanlines(&encoder, rows, 1);
    jpeg_finish_compress(&encoder);
    jpeg_destroy_compress(&encoder);
    return fclose(output) == 0 ? 0 : 1;
}
