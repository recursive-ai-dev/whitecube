/* SPDX-License-Identifier: MIT
 *
 * test_wire.c - unit tests for the wire codec and the sanitisation helpers.
 * Includes golden byte vectors, big-endian reads, truncation sweeps and a
 * deterministic fuzz pass (run it under ASan for maximum value).
 */
#include "dbus_wire.h"
#include "whitecube_portal.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static unsigned checks, failures;

#define CHECK(cond, ...)                                                      \
    do {                                                                      \
        checks++;                                                             \
        if (!(cond)) {                                                        \
            failures++;                                                       \
            printf("  FAIL %s:%d: ", __FILE__, __LINE__);                     \
            printf(__VA_ARGS__);                                              \
            printf("\n");                                                     \
        }                                                                     \
    } while (0)

static void test_signatures(void)
{
    static const char *const good[] = {
        "", "s", "u", "v", "as", "a{sv}", "a(sa(us))", "(ddd)", "a{sa{sv}}",
        "ssa{sv}", "ua{sv}", "ay", "a{sa{sa{sv}}}", "h", "aai", NULL
    };
    static const char *const bad[] = {
        "a", "{sv}", "(s", "s)", "a{vs}", "a{}", "x}", "a{sv", "()", "{s}",
        "a{s}", "vvv(", "a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a("
        "(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(a(u)"
        "))))))))))))))))))))))))))))))))", NULL
    };
    size_t i;
    char longsig[300];

    for (i = 0; good[i]; i++)
        CHECK(wc_sig_valid(good[i]), "should be valid: %s", good[i]);
    for (i = 0; bad[i]; i++)
        CHECK(!wc_sig_valid(bad[i]), "should be invalid: %s", bad[i]);

    memset(longsig, 'i', sizeof longsig - 1);
    longsig[sizeof longsig - 1] = 0;
    CHECK(!wc_sig_valid(longsig), "over-long signature accepted");

    CHECK(wc_sig_align("y") == 1, "align y");
    CHECK(wc_sig_align("n") == 2, "align n");
    CHECK(wc_sig_align("u") == 4, "align u");
    CHECK(wc_sig_align("s") == 4, "align s");
    CHECK(wc_sig_align("a{sv}") == 4, "align a");
    CHECK(wc_sig_align("(ddd)") == 8, "align struct");
    CHECK(wc_sig_align("t") == 8, "align t");
    CHECK(wc_sig_type_len("a(sa(us))") == 9, "type len a(sa(us))");
    CHECK(wc_sig_type_len("us") == 1, "type len of first type only");
}

static void test_golden_vectors(void)
{
    wc_writer w;
    /* v holding uint32 1: signature "u" then padding then the value. */
    static const uint8_t want_v_u32[] = { 0x01, 'u', 0x00, 0x00,
                                          0x01, 0x00, 0x00, 0x00 };
    static const uint8_t want_str[] = { 0x02, 0x00, 0x00, 0x00, 'a', 'b', 0x00 };
    static const uint8_t want_sig[] = { 0x05, 'a', '{', 's', 'v', '}', 0x00 };

    wc_w_init(&w);
    wc_w_variant(&w, "u");
    wc_w_u32(&w, 1);
    CHECK(wc_w_ok(&w) && w.len == sizeof want_v_u32 &&
          !memcmp(w.buf, want_v_u32, w.len), "golden v(u) mismatch (len=%zu)",
          w.len);
    wc_w_free(&w);

    wc_w_init(&w);
    wc_w_string(&w, "ab");
    CHECK(wc_w_ok(&w) && w.len == sizeof want_str &&
          !memcmp(w.buf, want_str, w.len), "golden string mismatch");
    wc_w_free(&w);

    wc_w_init(&w);
    wc_w_signature(&w, "a{sv}");
    CHECK(wc_w_ok(&w) && w.len == sizeof want_sig &&
          !memcmp(w.buf, want_sig, w.len), "golden signature mismatch");
    wc_w_free(&w);
}

static void test_roundtrip(void)
{
    wc_writer w;
    wc_reader r;
    wc_array_mark dict, arr;
    wc_array_iter it;
    size_t seen = 0;
    static const uint8_t bytes[] = { 1, 2, 3, 0 };

    wc_w_init(&w);
    dict = wc_w_dict_begin(&w);

    wc_w_dict_entry(&w, "str", "s");
    wc_w_string(&w, "hello world");
    wc_w_dict_entry(&w, "bool", "b");
    wc_w_bool(&w, true);
    wc_w_dict_entry(&w, "uint", "u");
    wc_w_u32(&w, 0xdeadbeefu);
    wc_w_dict_entry(&w, "dbl", "d");
    wc_w_double(&w, 0.4);
    wc_w_dict_entry(&w, "u64", "t");
    wc_w_u64(&w, 0x1122334455667788ull);
    wc_w_dict_entry(&w, "bytes", "ay");
    arr = wc_w_array_begin(&w, 1);
    for (size_t i = 0; i < sizeof bytes; i++)
        wc_w_byte(&w, bytes[i]);
    wc_w_array_end(&w, arr);
    wc_w_dict_entry(&w, "strs", "as");
    arr = wc_w_array_begin(&w, 4);
    wc_w_string(&w, "a");
    wc_w_string(&w, "b");
    wc_w_array_end(&w, arr);
    wc_w_dict_entry(&w, "accent", "(ddd)");
    wc_w_struct_begin(&w);
    wc_w_double(&w, 0.4);
    wc_w_double(&w, 0.7);
    wc_w_double(&w, 0.9);
    wc_w_dict_entry(&w, "nested", "a{sv}");
    {
        wc_array_mark inner = wc_w_dict_begin(&w);
        wc_w_dict_entry(&w, "x", "u");
        wc_w_u32(&w, 7);
        wc_w_dict_end(&w, inner);
    }
    wc_w_dict_end(&w, dict);
    CHECK(wc_w_ok(&w), "roundtrip marshal failed");

    wc_r_init(&r, w.buf, w.len, false);
    it = wc_r_array_begin(&r, 8);
    while (wc_r_array_more(&r, it)) {
        const char *key;
        wc_value v;
        wc_r_struct_begin(&r);
        key = wc_r_string(&r);
        CHECK(key != NULL, "null key");
        if (!key)
            break;
        if (!strcmp(key, "accent")) {
            const char *sig = wc_r_variant(&r);
            double a, b, c;
            CHECK(sig && !strcmp(sig, "(ddd)"), "accent sig");
            wc_r_struct_begin(&r);
            a = wc_r_double(&r);
            b = wc_r_double(&r);
            c = wc_r_double(&r);
            wc_r_struct_end(&r);
            CHECK(a == 0.4 && b == 0.7 && c == 0.9, "accent values");
        } else if (!strcmp(key, "nested")) {
            const char *sig = wc_r_variant(&r);
            wc_array_iter in;
            CHECK(sig && !strcmp(sig, "a{sv}"), "nested sig");
            in = wc_r_array_begin(&r, 8);
            while (wc_r_array_more(&r, in)) {
                const char *k2;
                wc_value v2;
                wc_r_struct_begin(&r);
                k2 = wc_r_string(&r);
                CHECK(k2 && !strcmp(k2, "x"), "nested key");
                CHECK(wc_value_read(&r, &v2) && v2.u32 == 7, "nested value");
                wc_r_struct_end(&r);
            }
            wc_r_array_end(&r, in);
        } else {
            CHECK(wc_value_read(&r, &v), "value read %s", key);
            if (!strcmp(key, "str"))
                CHECK(v.str && !strcmp(v.str, "hello world"), "str value");
            else if (!strcmp(key, "bool"))
                CHECK(v.b, "bool value");
            else if (!strcmp(key, "uint"))
                CHECK(v.u32 == 0xdeadbeefu, "uint value");
            else if (!strcmp(key, "dbl"))
                CHECK(v.dbl == 0.4, "double value");
            else if (!strcmp(key, "u64"))
                CHECK(v.u64 == 0x1122334455667788ull, "u64 value");
            else if (!strcmp(key, "bytes"))
                CHECK(v.bytes.len == 4 &&
                      !memcmp(v.bytes.data, bytes, 4), "ay value");
            else if (!strcmp(key, "strs"))
                CHECK(v.strs.count == 2 && !strcmp(v.strs.item[0], "a") &&
                      !strcmp(v.strs.item[1], "b"), "as value");
        }
        wc_r_struct_end(&r);
        seen++;
    }
    wc_r_array_end(&r, it);
    CHECK(wc_r_ok(&r), "roundtrip parse error");
    CHECK(seen == 9, "expected 9 dict entries, saw %zu", seen);
    CHECK(wc_r_left(&r) == 0, "trailing bytes: %zu", wc_r_left(&r));
    wc_w_free(&w);
}

static void test_big_endian(void)
{
    /* uint32 0x01020304 followed by string "hi", big endian. */
    static const uint8_t be[] = { 0x01, 0x02, 0x03, 0x04,
                                  0x00, 0x00, 0x00, 0x02, 'h', 'i', 0x00 };
    wc_reader r;
    const char *s;

    wc_r_init(&r, be, sizeof be, true);
    CHECK(wc_r_u32(&r) == 0x01020304u, "big endian u32");
    s = wc_r_string(&r);
    CHECK(s && !strcmp(s, "hi"), "big endian string");
    CHECK(wc_r_ok(&r), "big endian parse error");
}

static void parse_dict_blob(const uint8_t *data, size_t len)
{
    wc_reader r;
    wc_array_iter it;

    wc_r_init(&r, data, len, false);
    it = wc_r_array_begin(&r, 8);
    while (wc_r_array_more(&r, it)) {
        const char *key;
        wc_value v;
        wc_r_struct_begin(&r);
        key = wc_r_string(&r);
        if (!key)
            break;
        if (!wc_value_read(&r, &v))
            break;
        wc_r_struct_end(&r);
    }
    wc_r_array_end(&r, it);
}

static void test_truncation_and_fuzz(void)
{
    wc_writer w;
    wc_array_mark dict;
    size_t i;
    uint32_t seed = 0x12345678u;

    wc_w_init(&w);
    dict = wc_w_dict_begin(&w);
    wc_w_dict_entry(&w, "s", "s");
    wc_w_string(&w, "value");
    wc_w_dict_entry(&w, "n", "a{sv}");
    {
        wc_array_mark in = wc_w_dict_begin(&w);
        wc_w_dict_entry(&w, "k", "as");
        {
            wc_array_mark a = wc_w_array_begin(&w, 4);
            wc_w_string(&w, "x");
            wc_w_array_end(&w, a);
        }
        wc_w_dict_end(&w, in);
    }
    wc_w_dict_end(&w, dict);
    CHECK(wc_w_ok(&w), "fuzz corpus marshal");

    /* Every possible truncation must be rejected cleanly, never crash. */
    for (i = 0; i <= w.len; i++)
        parse_dict_blob(w.buf, i);

    /* Deterministic byte fuzzing. */
    for (i = 0; i < 20000; i++) {
        uint8_t blob[128];
        size_t n, j;
        seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
        n = (size_t)(seed % sizeof blob);
        for (j = 0; j < n; j++) {
            seed ^= seed << 13; seed ^= seed >> 17; seed ^= seed << 5;
            blob[j] = (uint8_t)(seed & 0xff);
        }
        parse_dict_blob(blob, n);
        (void)wc_r_skip(&(wc_reader){0}, "a{sv}");
    }
    /* Flipping single bytes of a valid message must also be survivable. */
    for (i = 0; i < w.len; i++) {
        uint8_t saved = w.buf[i];
        w.buf[i] ^= 0xff;
        parse_dict_blob(w.buf, w.len);
        w.buf[i] = saved;
    }
    wc_w_free(&w);
    CHECK(true, "truncation + fuzz sweep completed without crashing");
}

static void test_uri_encoding(void)
{
    char out[WC_URI_MAX];

    struct { const char *in, *want; } cases[] = {
        { "/tmp/plain.txt",        "file:///tmp/plain.txt" },
        { "/tmp/a b.txt",          "file:///tmp/a%20b.txt" },
        { "/tmp/a#b?c%d",          "file:///tmp/a%23b%3Fc%25d" },
        { "/tmp/caf\xc3\xa9",      "file:///tmp/caf%C3%A9" },
        { "/tmp/a+b~c-d_e.f",      "file:///tmp/a%2Bb~c-d_e.f" },
        { "/tmp/qu'\"x",           "file:///tmp/qu%27%22x" },
        { "/tmp/a&b=c",            "file:///tmp/a%26b%3Dc" },
        { "/tmp/\xe6\x97\xa5",     "file:///tmp/%E6%97%A5" },
        { "/tmp/back\\slash",      "file:///tmp/back%5Cslash" },
        { "/tmp/[br](x)",          "file:///tmp/%5Bbr%5D%28x%29" },
        { "/",                     "file:///" },
        { NULL, NULL }
    };
    size_t i;

    for (i = 0; cases[i].in; i++) {
        bool ok = wc_uri_encode(cases[i].in, out, sizeof out);
        CHECK(ok && !strcmp(out, cases[i].want), "uri(%s) = %s, want %s",
              cases[i].in, ok ? out : "(fail)", cases[i].want);
    }
    CHECK(!wc_uri_encode("relative/path", out, sizeof out), "relative accepted");
    CHECK(!wc_uri_encode("", out, sizeof out), "empty accepted");
    CHECK(!wc_uri_encode("/tmp/x", out, 8), "tiny buffer accepted");
    /* Worst case expansion must fit in WC_URI_MAX. */
    {
        char big[PATH_MAX];
        size_t k;
        for (k = 0; k < sizeof big - 2; k++)
            big[k] = (k % 2) ? ' ' : 'a';
        big[0] = '/';
        big[sizeof big - 2] = 0;
        CHECK(wc_uri_encode(big, out, sizeof out), "worst case expansion");
    }
}

static void test_sanitizers(void)
{
    char out[256];

    CHECK(wc_sender_element(":1.42", out, sizeof out) &&
          !strcmp(out, "1_42"), "sender :1.42 -> %s", out);
    CHECK(wc_sender_element(":1.100", out, sizeof out) &&
          !strcmp(out, "1_100"), "sender :1.100 -> %s", out);
    CHECK(!wc_sender_element("", out, sizeof out), "empty sender accepted");

    wc_sanitize_token("gtk1234", 7, out, sizeof out);
    CHECK(!strcmp(out, "gtk1234"), "token passthrough: %s", out);
    wc_sanitize_token("bad/../tok", 7, out, sizeof out);
    CHECK(!strcmp(out, "bad____tok"), "token sanitised: %s", out);
    wc_sanitize_token("", 7, out, sizeof out);
    CHECK(!strcmp(out, "wc7"), "empty token fallback: %s", out);
    wc_sanitize_token("../../etc/passwd", 7, out, sizeof out);
    CHECK(!strchr(out, '/') && !strstr(out, ".."), "traversal token: %s", out);

    wc_sanitize_text(out, sizeof out, "a\nb\tc\rd\x01" "e", 0);
    CHECK(!strcmp(out, "abcde"), "control chars stripped: %s", out);
    CHECK(!wc_text_is_safe("a\nb"), "newline considered safe");
    CHECK(wc_text_is_safe("/tmp/a b"), "space considered unsafe");
    CHECK(!wc_text_is_safe(""), "empty considered safe");
}

int main(void)
{
    printf("whitecube-portal unit tests\n");
    test_signatures();
    test_golden_vectors();
    test_roundtrip();
    test_big_endian();
    test_truncation_and_fuzz();
    test_uri_encoding();
    test_sanitizers();
    printf("%u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}