// Hardware-free unit tests for cube-audio: volume mapping, WAV decoding, resampling.
#define CUBE_AUDIO_NO_MAIN
#include "cube-audio.c"

static int fails;
#define CHECK(c) do { if (!(c)) { fprintf(stderr, "FAIL %s:%d %s\n", __FILE__, __LINE__, #c); fails++; } } while (0)

static void put32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static void put16(uint8_t *p, uint16_t v) { p[0] = v; p[1] = v >> 8; }

static void write_wav(const char *path, unsigned fmt, unsigned ch, unsigned rate, unsigned bits, const void *data, unsigned len) {
	uint8_t h[44] = "RIFFxxxxWAVEfmt ";
	put32(h + 4, 36 + len); put32(h + 16, 16); put16(h + 20, fmt); put16(h + 22, ch); put32(h + 24, rate);
	put32(h + 28, rate * ch * bits / 8); put16(h + 32, ch * bits / 8); put16(h + 34, bits);
	memcpy(h + 36, "data", 4); put32(h + 40, len);
	FILE *f = fopen(path, "wb");
	fwrite(h, 1, 44, f); fwrite(data, 1, len, f); fclose(f);
}

static struct volume mk(long min, long max, bool db, int dmin, int dmax, int step) {
	struct volume v;
	memset(&v, 0, sizeof v);
	v.vol.min = min; v.vol.max = max;
	v.db = (struct db_range){ db, dmin, dmax, step };
	return v;
}

int main(void) {
	// A dB-scaled control like HDA's: 0..87 raw, -65.25 dB..0 dB in 0.75 dB steps.
	struct volume v = mk(0, 87, true, -6525, 0, 75);
	CHECK(pct_from_raw(&v, 0) == 0);
	CHECK(pct_from_raw(&v, 87) == 100);
	CHECK(raw_from_pct(&v, 0) == 0);
	CHECK(raw_from_pct(&v, 100) == 87);
	CHECK(raw_from_pct(&v, 1) >= 1);                       // nonzero percent never lands on the mute step
	int prev = -1;
	for (int p = 0; p <= 100; p++) {
		long raw = raw_from_pct(&v, p);
		CHECK(raw >= prev);                               // monotone
		prev = (int)raw;
		CHECK(abs(pct_from_raw(&v, raw) - p) <= 2);       // round trip within quantisation
	}
	CHECK(raw_from_pct(&v, 50) > 40 && raw_from_pct(&v, 50) < 87);   // half volume is well above half the raw scale
	// linear control with no dB data
	struct volume l = mk(0, 100, false, 0, 0, 0);
	for (int p = 0; p <= 100; p++) CHECK(pct_from_raw(&l, raw_from_pct(&l, p)) == p);
	// degenerate control
	struct volume d = mk(5, 5, false, 0, 0, 0);
	CHECK(pct_from_raw(&d, 5) == 100 && raw_from_pct(&d, 50) == 5);

	// WAV decoding
	char err[200];
	struct wav w;
	int16_t s16[4] = { 16384, -16384, 32767, -32768 };
	write_wav("t16.wav", 1, 2, 48000, 16, s16, sizeof s16);
	CHECK(wav_open(&w, "t16.wav", err, sizeof err));
	CHECK(w.channels == 2 && w.rate == 48000 && w.data_left == 8);
	uint8_t raw[8]; CHECK(fread(raw, 1, 8, w.f) == 8);
	float L, R; wav_frame(&w, raw, &L, &R);
	CHECK(fabsf(L - 0.5f) < 1e-4f && fabsf(R + 0.5f) < 1e-4f);
	fclose(w.f);

	uint8_t u8[2] = { 128, 255 };
	write_wav("t8.wav", 1, 1, 8000, 8, u8, 2);
	CHECK(wav_open(&w, "t8.wav", err, sizeof err));
	wav_frame(&w, u8, &L, &R);
	CHECK(L == 0.0f && R == 0.0f);                         // mono duplicates to both channels
	wav_frame(&w, u8 + 1, &L, &R);
	CHECK(L > 0.99f && L == R);
	fclose(w.f);

	float f32[1] = { 0.25f };
	write_wav("tf.wav", 3, 1, 44100, 32, f32, 4);
	CHECK(wav_open(&w, "tf.wav", err, sizeof err));
	wav_frame(&w, (uint8_t *)f32, &L, &R);
	CHECK(L == 0.25f);
	fclose(w.f);

	uint8_t s24[3] = { 0x00, 0x00, 0x40 };                 // +0.5
	write_wav("t24.wav", 1, 1, 48000, 24, s24, 3);
	CHECK(wav_open(&w, "t24.wav", err, sizeof err));
	wav_frame(&w, s24, &L, &R);
	CHECK(fabsf(L - 0.5f) < 1e-4f);
	fclose(w.f);

	CHECK(!wav_open(&w, "../cube-audio.c", err, sizeof err) && strstr(err, "RIFF"));
	write_wav("tbad.wav", 2, 2, 48000, 4, s16, 8);          // ADPCM
	CHECK(!wav_open(&w, "tbad.wav", err, sizeof err) && strstr(err, "unsupported"));
	FILE *tr = fopen("ttrunc.wav", "wb"); fwrite("RIFF\0\0\0\0WAVE", 1, 12, tr); fclose(tr);
	CHECK(!wav_open(&w, "ttrunc.wav", err, sizeof err));

	// Resampler: 1 s of 24 kHz constant becomes ~48000 frames; 96 kHz becomes ~48000; equal rate is 1:1.
	struct conv c;
	int16_t out[2 * 64];
	unsigned up = 0, down = 0, same = 0;
	conv_init(&c, 24000, 48000);
	for (int i = 0; i < 24000; i++) { up += conv_push(&c, 0.5f, 0.5f, out); }
	CHECK(abs((int)up - 48000) <= 2);
	conv_init(&c, 96000, 48000);
	for (int i = 0; i < 96000; i++) { down += conv_push(&c, 0.5f, -0.5f, out); }
	CHECK(abs((int)down - 48000) <= 2);
	conv_init(&c, 48000, 48000);
	for (int i = 0; i < 100; i++) same += conv_push(&c, 0.0f, 0.0f, out);
	CHECK(same == 100);
	// interpolation is linear and bounded: a ramp 0 -> 1 upsampled 2x never overshoots
	conv_init(&c, 1, 2);
	conv_push(&c, 0.0f, 0.0f, out);
	unsigned n = conv_push(&c, 1.0f, 1.0f, out);
	CHECK(n == 2 && out[0] == 0 && abs(out[2] - 16384) <= 1);
	CHECK(to_s16(2.0f) == 32767 && to_s16(-2.0f) == -32767 && to_s16(NAN) == 0);

	remove("t16.wav"); remove("t8.wav"); remove("tf.wav"); remove("t24.wav"); remove("tbad.wav"); remove("ttrunc.wav");
	if (fails) { fprintf(stderr, "%d failure(s)\n", fails); return 1; }
	puts("cube-audio: volume mapping, WAV decoding and resampling passed");
	return 0;
}
