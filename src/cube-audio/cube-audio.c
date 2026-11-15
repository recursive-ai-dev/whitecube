// cube-audio: Whitecube's own audio layer.
//
// Talks to the kernel sound drivers directly through their device nodes
// (/dev/snd/pcmC*D*p, /dev/snd/controlC*) with ioctls from the kernel UAPI header
// <sound/asound.h>. It links nothing but libc and libm: no alsa-lib, no PipeWire,
// no PulseAudio, no WirePlumber. The kernel driver (snd_hda_intel, snd-usb-audio...)
// is the only thing between this program and the hardware.
//
//   cube-audio cards [--json]                 list cards and playback/capture devices
//   cube-audio status [-D card,dev] [-c CTL] [--input] [--json]
//   cube-audio volume [-D card,dev] [-c CTL] [--input] [--json] [N|+N|-N]
//   cube-audio mute   [-D card,dev] [-c CTL] [--input] [--json] [on|off|toggle]
//   cube-audio info   [--json] FILE.wav       inspect WAV metadata without hardware
//   cube-audio play   [-D card,dev] FILE.wav  play 8/16/24/32 PCM or 32/64 float WAV
//   cube-audio selftest [-D card,dev]         probe hardware; plays 250 ms of silence
//
// Device choice: -D card,dev, else $CUBE_AUDIO_DEVICE, else the first card that has
// a playback volume control and a playback PCM.
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <sound/asound.h>
#include <sound/tlv.h>

#define PROG "cube-audio"
#ifndef WHITECUBE_VERSION
#define WHITECUBE_VERSION "0.12.0"
#endif
#define MAX_CARDS 32
#define MAX_ELEMS 65536u

static volatile sig_atomic_t g_stop;
static void on_signal(int s) { (void)s; g_stop = 1; }

// ------------------------------------------------------------------ utilities

static int die(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static int die(const char *fmt, ...) {
	va_list ap;
	fprintf(stderr, PROG ": ");
	va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap);
	fputc('\n', stderr);
	return 1;
}

static int clampi(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }
static double clampd(double v, double lo, double hi) { return v < lo ? lo : v > hi ? hi : v; }

static void json_print_str(const char *s) {
	putchar('"');
	for (const unsigned char *p = (const unsigned char *)(s ? s : ""); *p; p++) {
		if (*p == '"' || *p == '\\') { putchar('\\'); putchar(*p); }
		else if (*p == '\n') fputs("\\n", stdout);
		else if (*p == '\r') fputs("\\r", stdout);
		else if (*p == '\t') fputs("\\t", stdout);
		else if (*p < 0x20) printf("\\u%04x", *p);
		else putchar(*p);
	}
	putchar('"');
}

// ------------------------------------------------------------------- mixer

struct ctl_elem {
	struct snd_ctl_elem_id id;
	unsigned type, count, access;
	long min, max, step;
};

struct mixer {
	int fd, card;
	char name[32], longname[80];
	struct snd_ctl_elem_id *ids;
	unsigned n;
};

static int mixer_open(struct mixer *m, int card) {
	memset(m, 0, sizeof *m);
	m->fd = -1;
	char path[48];
	snprintf(path, sizeof path, "/dev/snd/controlC%d", card);
	m->fd = open(path, O_RDWR | O_CLOEXEC);
	if (m->fd < 0) return -errno;
	m->card = card;
	struct snd_ctl_card_info ci;
	memset(&ci, 0, sizeof ci);
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_CARD_INFO, &ci) == 0) {
		snprintf(m->name, sizeof m->name, "%.*s", (int)sizeof ci.id, (const char *)ci.id);
		snprintf(m->longname, sizeof m->longname, "%.*s", (int)sizeof ci.name, (const char *)ci.name);
	}
	struct snd_ctl_elem_list l;
	memset(&l, 0, sizeof l);
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_LIST, &l) < 0) { int e = errno; close(m->fd); m->fd = -1; return -e; }
	if (l.count > MAX_ELEMS) { close(m->fd); m->fd = -1; return -E2BIG; }
	if (l.count) {
		m->ids = calloc(l.count, sizeof *m->ids);
		if (!m->ids) { close(m->fd); m->fd = -1; return -ENOMEM; }
		l.space = l.count; l.pids = m->ids;
		if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_LIST, &l) < 0) {
			int e = errno; free(m->ids); m->ids = NULL; close(m->fd); m->fd = -1; return -e;
		}
		m->n = l.used < l.count ? l.used : l.count;
	}
	return 0;
}

static void mixer_close(struct mixer *m) { if (m->fd >= 0) close(m->fd); free(m->ids); m->ids = NULL; m->fd = -1; }

static bool mixer_find(const struct mixer *m, const char *name, struct ctl_elem *out) {
	size_t namelen = strlen(name);
	for (unsigned i = 0; i < m->n; i++) {
		const struct snd_ctl_elem_id *id = &m->ids[i];
		size_t idlen = strnlen((const char *)id->name, sizeof id->name);
		if (id->iface != SNDRV_CTL_ELEM_IFACE_MIXER || idlen != namelen || memcmp(id->name, name, namelen)) continue;
		struct snd_ctl_elem_info inf;
		memset(&inf, 0, sizeof inf);
		inf.id.numid = id->numid;
		if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_INFO, &inf) < 0) continue;
		if (inf.count == 0 || inf.count > 128) continue;
		memset(out, 0, sizeof *out);
		out->id = inf.id; out->type = inf.type; out->count = inf.count; out->access = inf.access;
		if (inf.type == SNDRV_CTL_ELEM_TYPE_INTEGER) {
			out->min = inf.value.integer.min; out->max = inf.value.integer.max; out->step = inf.value.integer.step;
		}
		return true;
	}
	return false;
}

static int elem_read(const struct mixer *m, const struct ctl_elem *e, long *vals) {
	struct snd_ctl_elem_value *v = calloc(1, sizeof *v);
	if (!v) return -ENOMEM;
	v->id = e->id;
	int r = ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_READ, v) < 0 ? -errno : 0;
	if (!r) for (unsigned i = 0; i < e->count && i < 128; i++) vals[i] = v->value.integer.value[i];
	free(v);
	return r;
}

static int elem_write_all(const struct mixer *m, const struct ctl_elem *e, long val) {
	struct snd_ctl_elem_value *v = calloc(1, sizeof *v);
	if (!v) return -ENOMEM;
	v->id = e->id;
	for (unsigned i = 0; i < e->count && i < 128; i++) v->value.integer.value[i] = val;
	int r = ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_WRITE, v) < 0 ? -errno : 0;
	free(v);
	return r;
}

// The dB description of a volume control, if the driver publishes one (TLV).
struct db_range { bool ok; int min, max, step; };   // centi-dB; step 0 = interpolate raw

static struct db_range db_scale_range(int min_db, int step_db, long raw_min, long raw_max) {
	struct db_range d = {0};
	if (step_db <= 0 || raw_max <= raw_min) return d;
	int64_t span = (int64_t)raw_max - (int64_t)raw_min;
	if (span > 1000000) return d;
	int64_t max_db = (int64_t)min_db + (int64_t)step_db * span;
	if (max_db < -1000000 || max_db > 1000000 || max_db <= min_db) return d;
	d.min = min_db; d.step = step_db; d.max = (int)max_db; d.ok = true;
	return d;
}

static struct db_range tlv_parse_db(const unsigned *tlv, size_t nwords, long raw_min, long raw_max, int depth) {
	struct db_range d = {0};
	if (!tlv || depth > 2 || nwords < 2) return d;
	unsigned type = tlv[0];
	unsigned bytes = tlv[1];
	size_t payload_words = bytes / sizeof(unsigned);
	if ((bytes % sizeof(unsigned)) != 0 || payload_words > nwords - 2) return d;
	if (type == SNDRV_CTL_TLVT_CONTAINER) {
		size_t off = 2;
		while (off + 2 <= 2 + payload_words) {
			unsigned sub_bytes = tlv[off + 1];
			size_t sub_words = 2 + (((size_t)sub_bytes + sizeof(unsigned) - 1) / sizeof(unsigned));
			if (sub_words < 2 || sub_words > (2 + payload_words) - off) break;
			d = tlv_parse_db(tlv + off, sub_words, raw_min, raw_max, depth + 1);
			if (d.ok) return d;
			off += sub_words;
		}
		return d;
	}
	if (payload_words < 2) return d;
	if (type == SNDRV_CTL_TLVT_DB_SCALE) {
		d = db_scale_range((int)tlv[2], (int)(tlv[3] & 0xffff), raw_min, raw_max);
	} else if (type == SNDRV_CTL_TLVT_DB_MINMAX || type == SNDRV_CTL_TLVT_DB_MINMAX_MUTE ||
	           type == SNDRV_CTL_TLVT_DB_LINEAR) {
		d.min = (int)tlv[2]; d.max = (int)tlv[3];
		d.ok = d.max > d.min && d.min >= -1000000 && d.max <= 1000000;
	}
	return d;
}

static struct db_range elem_db(const struct mixer *m, const struct ctl_elem *e) {
	struct db_range d = {0};
	if (!(e->access & SNDRV_CTL_ELEM_ACCESS_TLV_READ)) return d;
	unsigned buf[2 + 64];
	struct snd_ctl_tlv *t = (struct snd_ctl_tlv *)buf;
	memset(buf, 0, sizeof buf);
	t->numid = e->id.numid; t->length = sizeof buf - sizeof *t;
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_TLV_READ, t) < 0 || t->length < 16 || t->length > sizeof buf - sizeof *t) return d;
	return tlv_parse_db(t->tlv, t->length / sizeof(unsigned), e->min, e->max, 0);
}

// A user-facing volume: one integer control plus its optional on/off switch.
struct volume {
	struct mixer mx;
	struct ctl_elem vol, sw;
	bool has_sw;
	struct db_range db;
	char label[48];
};

// Percent is linear in dB over at most the top 60 dB, so 50% sounds half as loud
// rather than the lower half of a raw scale nobody can hear. 0% is the raw minimum.
#define DB_SPAN_MAX 6000

static double db_at_raw(const struct volume *v, long raw) {
	if (v->db.step) return v->db.min + (double)v->db.step * (double)(raw - v->vol.min);
	return v->db.min + (double)(v->db.max - v->db.min) * (double)(raw - v->vol.min) / (double)(v->vol.max - v->vol.min);
}

static int pct_from_raw(const struct volume *v, long raw) {
	if (v->vol.max <= v->vol.min) return 100;
	raw = raw < v->vol.min ? v->vol.min : raw > v->vol.max ? v->vol.max : raw;
	if (!v->db.ok) return (int)lround(100.0 * (double)(raw - v->vol.min) / (double)(v->vol.max - v->vol.min));
	if (raw == v->vol.min) return 0;
	double span = fmin((double)(v->db.max - v->db.min), DB_SPAN_MAX);
	double p = 1.0 - ((double)v->db.max - db_at_raw(v, raw)) / span;
	return clampi((int)lround(clampd(p, 0, 1) * 100.0), 0, 100);
}

static long raw_from_pct(const struct volume *v, int pct) {
	pct = clampi(pct, 0, 100);
	if (v->vol.max <= v->vol.min || pct == 0) return v->vol.min;
	if (!v->db.ok) return v->vol.min + lround((double)(v->vol.max - v->vol.min) * pct / 100.0);
	double span = fmin((double)(v->db.max - v->db.min), DB_SPAN_MAX);
	double db = v->db.max - (1.0 - pct / 100.0) * span;
	double frac = (db - v->db.min) / (double)(v->db.max - v->db.min);
	long raw = v->vol.min + lround(frac * (double)(v->vol.max - v->vol.min));
	if (raw <= v->vol.min) raw = v->vol.min + 1;      // nonzero percent never lands on the mute step
	return raw > v->vol.max ? v->vol.max : raw;
}

static const char *const OUT_CONTROLS[] = { "Master", "PCM", "Speaker", "Headphone", NULL };
static const char *const IN_CONTROLS[]  = { "Capture", "Mic", NULL };

static bool volume_match(const struct mixer *m, const char *ctl, const char *dir, struct volume *v) {
	char name[64];
	if (mixer_find(m, ctl, &v->vol) && v->vol.type == SNDRV_CTL_ELEM_TYPE_INTEGER) {
		snprintf(v->label, sizeof v->label, "%s", ctl);
		char *p = strstr(v->label, " Volume");
		if (p) *p = 0;
		snprintf(name, sizeof name, "%s", ctl);
		char *vp = strstr(name, " Volume");
		if (vp) memcpy(vp, " Switch", 8);
		else snprintf(name, sizeof name, "%s %s Switch", ctl, dir);
		v->has_sw = mixer_find(m, name, &v->sw) && v->sw.type == SNDRV_CTL_ELEM_TYPE_BOOLEAN;
		v->db = elem_db(m, &v->vol);
		return true;
	}
	snprintf(name, sizeof name, "%s %s Volume", ctl, dir);
	if (!strcmp(dir, "Capture") && !strcmp(ctl, "Capture")) snprintf(name, sizeof name, "Capture Volume");
	if (!mixer_find(m, name, &v->vol) || v->vol.type != SNDRV_CTL_ELEM_TYPE_INTEGER) return false;
	snprintf(v->label, sizeof v->label, "%s", ctl);
	snprintf(name, sizeof name, "%s %s Switch", ctl, dir);
	if (!strcmp(dir, "Capture") && !strcmp(ctl, "Capture")) snprintf(name, sizeof name, "Capture Switch");
	v->has_sw = mixer_find(m, name, &v->sw) && v->sw.type == SNDRV_CTL_ELEM_TYPE_BOOLEAN;
	v->db = elem_db(m, &v->vol);
	return true;
}

static bool volume_bind_control(struct volume *v, int card, bool input, const char *control) {
	if (mixer_open(&v->mx, card) < 0) return false;
	const char *dir = input ? "Capture" : "Playback";
	if (control && *control) {
		if (volume_match(&v->mx, control, dir, v)) return true;
		mixer_close(&v->mx);
		return false;
	}
	for (const char *const *b = input ? IN_CONTROLS : OUT_CONTROLS; *b; b++) {
		if (volume_match(&v->mx, *b, dir, v)) return true;
	}
	mixer_close(&v->mx);
	return false;
}

static bool volume_bind(struct volume *v, int card, bool input) {
	return volume_bind_control(v, card, input, NULL);
}

static int volume_get(const struct volume *v) {
	long vals[128] = {0};
	unsigned n = v->vol.count < 128 ? v->vol.count : 128;
	if (!n || elem_read(&v->mx, &v->vol, vals) < 0) return -1;
	long long sum = 0;
	for (unsigned i = 0; i < n; i++) sum += pct_from_raw(v, vals[i]);
	return (int)(sum / n);
}

static int volume_set(const struct volume *v, int pct) { return elem_write_all(&v->mx, &v->vol, raw_from_pct(v, pct)); }

// 1 muted, 0 audible, -1 no switch / unreadable
static int volume_muted(const struct volume *v) {
	if (!v->has_sw) return -1;
	long vals[128] = {0};
	if (elem_read(&v->mx, &v->sw, vals) < 0) return -1;
	for (unsigned i = 0; i < v->sw.count && i < 128; i++) if (vals[i]) return 0;
	return 1;
}

static int volume_set_muted(const struct volume *v, bool muted) {
	return v->has_sw ? elem_write_all(&v->mx, &v->sw, muted ? 0 : 1) : -ENOTSUP;
}

// ------------------------------------------------------------- card listing

static bool card_exists(int card) {
	char p[48];
	snprintf(p, sizeof p, "/dev/snd/controlC%d", card);
	return access(p, F_OK) == 0;
}

static bool pcm_exists(int card, int dev, bool capture) {
	char p[64];
	snprintf(p, sizeof p, "/dev/snd/pcmC%dD%d%c", card, dev, capture ? 'c' : 'p');
	return access(p, F_OK) == 0;
}

struct target { int card, dev; };

static bool parse_target(const char *s, struct target *t) {
	int c, d;
	char tail;
	if (sscanf(s, "%d,%d%c", &c, &d, &tail) == 2 && c >= 0 && d >= 0) { t->card = c; t->dev = d; return true; }
	return false;
}

static bool default_target(struct target *t, bool capture) {
	const char *env = getenv("CUBE_AUDIO_DEVICE");
	if (env && parse_target(env, t)) return true;
	int fallback_card = -1;
	for (int c = 0; c < MAX_CARDS; c++) {
		if (!card_exists(c)) continue;
		if (!pcm_exists(c, 0, capture)) continue;
		struct volume v;
		bool has = volume_bind(&v, c, capture);
		if (has) mixer_close(&v.mx);
		if (has) { t->card = c; t->dev = 0; return true; }
		if (fallback_card < 0) fallback_card = c;
	}
	if (fallback_card >= 0) { t->card = fallback_card; t->dev = 0; return true; }
	return false;
}

static int cmd_cards(bool json) {
	int found = 0;
	if (json) putchar('[');
	for (int c = 0; c < MAX_CARDS; c++) {
		struct mixer m;
		if (mixer_open(&m, c) < 0) continue;
		if (json) {
			if (found) putchar(',');
			printf("{\"card\":%d,\"name\":", c); json_print_str(m.name);
			printf(",\"longname\":"); json_print_str(m.longname);
			printf(",\"devices\":[");
		} else {
			printf("card %d: %s (%s)\n", c, m.name, m.longname);
		}
		found++;
		int ndev = 0;
		for (int pass = 0; pass < 2; pass++)
			for (int d = 0; d < 64; d++) {
				if (!pcm_exists(c, d, pass)) continue;
				struct snd_pcm_info pi;
				memset(&pi, 0, sizeof pi);
				pi.device = d; pi.stream = pass; pi.subdevice = 0;
				const char *name = ioctl(m.fd, SNDRV_CTL_IOCTL_PCM_INFO, &pi) == 0 ? (const char *)pi.name : "?";
				if (json) {
					if (ndev++) putchar(',');
					printf("{\"type\":\"%s\",\"card\":%d,\"device\":%d,\"name\":", pass ? "capture" : "playback", c, d);
					json_print_str(name); putchar('}');
				} else {
					printf("  %s %d,%d: %s\n", pass ? "capture " : "playback", c, d, name);
				}
			}
		if (json) printf("]}");
		mixer_close(&m);
	}
	if (json) { printf("]\n"); return found ? 0 : 1; }
	if (!found) return die("no sound cards found under /dev/snd (is a driver loaded, are you in the 'audio' group?)");
	return 0;
}

// --------------------------------------------------------------------- PCM

struct pcm {
	int fd;
	unsigned rate, channels;
	unsigned long period, buffer;     // frames
};

static struct snd_mask *pm(struct snd_pcm_hw_params *p, int var) { return &p->masks[var - SNDRV_PCM_HW_PARAM_FIRST_MASK]; }
static struct snd_interval *pi_(struct snd_pcm_hw_params *p, int var) { return &p->intervals[var - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL]; }

static void hw_any(struct snd_pcm_hw_params *p) {
	memset(p, 0, sizeof *p);
	for (int v = SNDRV_PCM_HW_PARAM_FIRST_MASK; v <= SNDRV_PCM_HW_PARAM_LAST_MASK; v++)
		memset(pm(p, v)->bits, 0xff, sizeof pm(p, v)->bits);
	for (int v = SNDRV_PCM_HW_PARAM_FIRST_INTERVAL; v <= SNDRV_PCM_HW_PARAM_LAST_INTERVAL; v++) {
		pi_(p, v)->min = 0; pi_(p, v)->max = UINT_MAX;
	}
	p->rmask = ~0u;
}

static void hw_set_mask(struct snd_pcm_hw_params *p, int var, unsigned bit) {
	struct snd_mask *m = pm(p, var);
	memset(m->bits, 0, sizeof m->bits);
	if (bit < sizeof(m->bits) * 8) m->bits[bit >> 5] = 1u << (bit & 31);
}

static void hw_set_interval(struct snd_pcm_hw_params *p, int var, unsigned lo, unsigned hi) {
	struct snd_interval *i = pi_(p, var);
	i->min = lo; i->max = hi; i->openmin = i->openmax = 0; i->integer = 1;
}

static int hw_refine(int fd, struct snd_pcm_hw_params *p) {
	p->rmask = ~0u;
	return ioctl(fd, SNDRV_PCM_IOCTL_HW_REFINE, p) < 0 ? -errno : 0;
}

// Pin an interval to a single value near `want`, keeping the kernel's constraints.
static int hw_pin(int fd, struct snd_pcm_hw_params *p, int var, unsigned want) {
	struct snd_interval *i = pi_(p, var);
	unsigned lo = i->min + (i->openmin ? 1 : 0), hi = i->max - (i->openmax ? 1 : 0);
	unsigned pick = want < lo ? lo : want > hi ? hi : want;
	struct snd_pcm_hw_params trial = *p;
	hw_set_interval(&trial, var, pick, pick);
	if (hw_refine(fd, &trial) == 0) { *p = trial; return 0; }
	trial = *p;
	hw_set_interval(&trial, var, lo, lo);
	int r = hw_refine(fd, &trial);
	if (r == 0) *p = trial;
	return r;
}

static const char *pcm_strerror(int e) {
	return e == EBUSY ? "device busy (another program has it open)" : e == ENOENT || e == ENODEV ? "no such device" :
	       e == EACCES ? "permission denied (are you in the 'audio' group?)" : strerror(e);
}

static int pcm_open_playback(struct pcm *pcm, struct target t, unsigned rate, unsigned channels) {
	memset(pcm, 0, sizeof *pcm);
	pcm->fd = -1;
	char path[64];
	snprintf(path, sizeof path, "/dev/snd/pcmC%dD%dp", t.card, t.dev);
	pcm->fd = open(path, O_WRONLY | O_CLOEXEC);
	if (pcm->fd < 0) return -errno;
	int proto = 0;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_PVERSION, &proto) < 0 || SNDRV_PROTOCOL_MAJOR(proto) != SNDRV_PROTOCOL_MAJOR(SNDRV_PCM_VERSION)) {
		close(pcm->fd); pcm->fd = -1; return -EPROTO;
	}
	struct snd_pcm_hw_params hp;
	unsigned rates[3] = { rate, 48000, 44100 };
	int err = -EINVAL;
	for (int k = 0; k < 3 && err; k++) {
		hw_any(&hp);
		hw_set_mask(&hp, SNDRV_PCM_HW_PARAM_ACCESS, SNDRV_PCM_ACCESS_RW_INTERLEAVED);
		hw_set_mask(&hp, SNDRV_PCM_HW_PARAM_FORMAT, SNDRV_PCM_FORMAT_S16_LE);
		hw_set_mask(&hp, SNDRV_PCM_HW_PARAM_SUBFORMAT, SNDRV_PCM_SUBFORMAT_STD);
		hw_set_interval(&hp, SNDRV_PCM_HW_PARAM_CHANNELS, channels, channels);
		hw_set_interval(&hp, SNDRV_PCM_HW_PARAM_RATE, rates[k], rates[k]);
		err = hw_refine(pcm->fd, &hp);
		if (!err) pcm->rate = rates[k];
	}
	if (err) { close(pcm->fd); pcm->fd = -1; return err; }
	pcm->channels = channels;
	// ~20 ms periods, four of them: low enough latency for a bell, safe against scheduling jitter.
	if ((err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, pcm->rate / 50)) ||
	    (err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_PERIODS, 4)) ||
	    (err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE, pi_(&hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min))) {
		close(pcm->fd); pcm->fd = -1; return err;
	}
	hp.rmask = ~0u;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_HW_PARAMS, &hp) < 0) { err = -errno; close(pcm->fd); pcm->fd = -1; return err; }
	pcm->period = pi_(&hp, SNDRV_PCM_HW_PARAM_PERIOD_SIZE)->min;
	pcm->buffer = pi_(&hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min;
	if (pcm->period == 0 || pcm->period > 262144 || pcm->buffer < pcm->period) {
		close(pcm->fd); pcm->fd = -1; return -EINVAL;
	}

	struct snd_pcm_sw_params sp;
	memset(&sp, 0, sizeof sp);
	sp.tstamp_mode = SNDRV_PCM_TSTAMP_NONE;
	sp.period_step = 1;
	sp.avail_min = pcm->period;
	sp.xfer_align = 1;
	sp.start_threshold = pcm->period * 2 < pcm->buffer ? pcm->period * 2 : pcm->buffer;
	sp.stop_threshold = pcm->buffer;       // an empty buffer is an underrun, reported as EPIPE
	sp.proto = SNDRV_PCM_VERSION;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_SW_PARAMS, &sp) < 0) { err = -errno; close(pcm->fd); pcm->fd = -1; return err; }
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE) < 0) { err = -errno; close(pcm->fd); pcm->fd = -1; return err; }
	return 0;
}

static int pcm_recover(struct pcm *pcm, int err) {
	if (err == EPIPE) return ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE) < 0 ? -errno : 0;
	if (err == ESTRPIPE) {
		while (!g_stop && ioctl(pcm->fd, SNDRV_PCM_IOCTL_RESUME) < 0 && errno == EAGAIN) usleep(10000);
		return ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE) < 0 ? -errno : 0;
	}
	return -err;
}

// Blocking write of interleaved S16_LE stereo frames. Recovers from underruns.
static int pcm_write(struct pcm *pcm, const int16_t *frames, unsigned long n) {
	unsigned retries = 0;
	while (n && !g_stop) {
		struct snd_xferi x = { .buf = (void *)frames, .frames = n };
		if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_WRITEI_FRAMES, &x) < 0) {
			int e = errno;
			if (e == EINTR) continue;
			if (++retries > 8) return -e;
			int r = pcm_recover(pcm, e);
			if (r) return r;
			continue;
		}
		retries = 0;
		frames += (unsigned long)x.result * pcm->channels;
		n -= (unsigned long)x.result;
	}
	return 0;
}

static int pcm_finish(struct pcm *pcm) {
	if (g_stop) { ioctl(pcm->fd, SNDRV_PCM_IOCTL_DROP); return 0; }
	for (;;) {
		if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_DRAIN) == 0) return 0;
		if (errno == EINTR && !g_stop) continue;
		if (g_stop) { ioctl(pcm->fd, SNDRV_PCM_IOCTL_DROP); return 0; }
		return -errno;
	}
}

static void pcm_close(struct pcm *pcm) { if (pcm->fd >= 0) close(pcm->fd); pcm->fd = -1; }

// --------------------------------------------------------------- WAV decode

struct wav {
	FILE *f;
	unsigned fmt, channels, rate, bits, align;
	uint64_t data_left;          // bytes
};

static uint32_t le32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint16_t le16(const uint8_t *p) { return p[0] | p[1] << 8; }

// Returns NULL and fills err with a reason.
static bool wav_open(struct wav *w, const char *path, char *err, size_t errn) {
	memset(w, 0, sizeof *w);
	w->f = fopen(path, "rb");
	if (!w->f) { snprintf(err, errn, "%s: %s", path, strerror(errno)); return false; }
	uint8_t h[12];
	if (fread(h, 1, 12, w->f) != 12 || memcmp(h, "RIFF", 4) || memcmp(h + 8, "WAVE", 4)) {
		snprintf(err, errn, "%s: not a RIFF/WAVE file", path); goto bad;
	}
	bool have_fmt = false;
	for (;;) {
		uint8_t ck[8];
		if (fread(ck, 1, 8, w->f) != 8) { snprintf(err, errn, "%s: no data chunk", path); goto bad; }
		uint32_t len = le32(ck + 4);
		if (!memcmp(ck, "fmt ", 4)) {
			uint8_t f[40];
			if (have_fmt || len < 16 || len > sizeof f || fread(f, 1, len, w->f) != len) { snprintf(err, errn, "%s: bad fmt chunk", path); goto bad; }
			w->fmt = le16(f); w->channels = le16(f + 2); w->rate = le32(f + 4); w->align = le16(f + 12); w->bits = le16(f + 14);
			if (w->fmt == 0xFFFE && len >= 26) w->fmt = le16(f + 24);     // WAVE_FORMAT_EXTENSIBLE sub-format
			have_fmt = true;
			if ((len & 1) && fgetc(w->f) == EOF) { snprintf(err, errn, "%s: truncated", path); goto bad; }
		} else if (!memcmp(ck, "data", 4)) {
			if (!have_fmt) { snprintf(err, errn, "%s: data before fmt", path); goto bad; }
			w->data_left = len;
			break;
		} else {
			int64_t skip = (int64_t)len + (len & 1), pos = ftello(w->f);
			struct stat st;
			if (pos < 0 || (fstat(fileno(w->f), &st) == 0 && S_ISREG(st.st_mode) &&
			                (pos > st.st_size || (uint64_t)skip > (uint64_t)(st.st_size - pos))) ||
			    fseeko(w->f, skip, SEEK_CUR) != 0) {
				snprintf(err, errn, "%s: truncated", path); goto bad;
			}
		}
	}
	bool pcm_ok = w->fmt == 1 && (w->bits == 8 || w->bits == 16 || w->bits == 24 || w->bits == 32);
	bool flt_ok = w->fmt == 3 && (w->bits == 32 || w->bits == 64);
	if (!pcm_ok && !flt_ok) { snprintf(err, errn, "%s: unsupported format %u/%u-bit (need PCM 8/16/24/32 or float 32/64)", path, w->fmt, w->bits); goto bad; }
	if (w->channels < 1 || w->channels > 8 || w->rate < 4000 || w->rate > 384000 || w->align != w->channels * (w->bits / 8)) {
		snprintf(err, errn, "%s: unsupported layout (%u ch, %u Hz)", path, w->channels, w->rate); goto bad;
	}
	if (w->data_left < w->align) {
		snprintf(err, errn, "%s: empty audio data", path); goto bad;
	}
	return true;
bad:
	fclose(w->f); w->f = NULL;
	return false;
}

// Decode one frame at p to stereo floats (mono is duplicated, surround keeps the front pair).
static void wav_frame(const struct wav *w, const uint8_t *p, float *l, float *r) {
	float s[2];
	unsigned bytes = w->bits / 8;
	for (int c = 0; c < 2; c++) {
		const uint8_t *q = p + (w->channels > 1 ? c : 0) * bytes;
		if (w->fmt == 3 && w->bits == 64) {
			uint64_t u = (uint64_t)le32(q) | ((uint64_t)le32(q + 4) << 32);
			double d; memcpy(&d, &u, 8); s[c] = (float)d;
		} else if (w->fmt == 3) { uint32_t u = le32(q); float f; memcpy(&f, &u, 4); s[c] = f; }
		else if (w->bits == 8) s[c] = ((int)q[0] - 128) / 128.0f;
		else if (w->bits == 16) s[c] = (int16_t)le16(q) / 32768.0f;
		else if (w->bits == 24) s[c] = ((int32_t)((uint32_t)q[0] << 8 | (uint32_t)q[1] << 16 | (uint32_t)q[2] << 24)) / 2147483648.0f;
		else s[c] = (int32_t)le32(q) / 2147483648.0f;
	}
	*l = s[0]; *r = s[1];
}

static int16_t to_s16(float f) {
	if (!(f == f)) return 0;
	f = f < -1.0f ? -1.0f : f > 1.0f ? 1.0f : f;
	return (int16_t)lrintf(f * 32767.0f);
}

// Streaming linear-interpolation resampler + stereo S16 packer.
// Maximum supported upsampling ratio is 384000 Hz / 4000 Hz = 96 frames per input frame.
#define CONV_MAX_FRAMES 128

struct conv {
	double step, pos;            // input frames per output frame; fractional position between prev and cur
	float pl, pr;
	bool have_prev, same_rate;
};

static void conv_init(struct conv *c, unsigned src, unsigned dst) {
	memset(c, 0, sizeof *c);
	if (!src) src = 48000;
	if (!dst) dst = 48000;
	c->same_rate = src == dst;
	c->step = (double)src / (double)dst;
	if (!(c->step >= 1.0 / CONV_MAX_FRAMES)) c->step = 1.0 / CONV_MAX_FRAMES;
}

// Push one input frame; writes 0..CONV_MAX_FRAMES output frames to out, returns the count.
static unsigned conv_push(struct conv *c, float l, float r, int16_t *out) {
	if (c->same_rate) { out[0] = to_s16(l); out[1] = to_s16(r); return 1; }
	unsigned n = 0;
	if (c->have_prev)
		while (c->pos < 1.0 && n < CONV_MAX_FRAMES) {
			out[n * 2] = to_s16(c->pl + (l - c->pl) * (float)c->pos);
			out[n * 2 + 1] = to_s16(c->pr + (r - c->pr) * (float)c->pos);
			n++; c->pos += c->step;
		}
	if (c->have_prev) c->pos -= 1.0;
	c->pl = l; c->pr = r; c->have_prev = true;
	return n;
}

static unsigned conv_flush(const struct conv *c, int16_t *out) {
	if (c->same_rate || !c->have_prev || c->pos >= 1.0) return 0;
	out[0] = to_s16(c->pl);
	out[1] = to_s16(c->pr);
	return 1;
}

static int cmd_info(const char *path, bool json) {
	struct wav w;
	char err[200];
	if (!wav_open(&w, path, err, sizeof err)) return die("%s", err);
	uint64_t frames = w.data_left / w.align;
	double duration = (double)frames / (double)w.rate;
	const char *fmt_name = w.fmt == 3 ? "float" : "pcm";
	fclose(w.f);
	if (json) {
		printf("{\"format\":\"%s\",\"bits\":%u,\"channels\":%u,\"rate\":%u,\"frames\":%llu,\"duration_s\":%.6f,\"bytes\":%llu}\n",
		       fmt_name, w.bits, w.channels, w.rate, (unsigned long long)frames, duration, (unsigned long long)w.data_left);
	} else {
		printf("format=%s\nbits=%u\nchannels=%u\nrate=%u\nframes=%llu\nduration=%.3fs\nbytes=%llu\n",
		       fmt_name, w.bits, w.channels, w.rate, (unsigned long long)frames, duration, (unsigned long long)w.data_left);
	}
	return 0;
}

static int cmd_play(struct target t, const char *path) {
	struct wav w;
	char err[200];
	if (!wav_open(&w, path, err, sizeof err)) return die("%s", err);
	struct pcm pcm;
	int r = pcm_open_playback(&pcm, t, w.rate, 2);
	if (r) { fclose(w.f); return die("cannot open playback %d,%d: %s", t.card, t.dev, pcm_strerror(-r)); }
	struct sigaction sa = { .sa_handler = on_signal };
	sigaction(SIGINT, &sa, NULL); sigaction(SIGTERM, &sa, NULL);

	struct conv cv;
	conv_init(&cv, w.rate, pcm.rate);
	size_t chunk_frames = 4096;
	size_t period_frames = pcm.period > CONV_MAX_FRAMES ? pcm.period : CONV_MAX_FRAMES;
	if (period_frames > SIZE_MAX / 8) { pcm_close(&pcm); fclose(w.f); return die("period size overflow"); }
	size_t bufn = period_frames * 2 * 4;
	uint8_t *in = malloc(chunk_frames * w.align);
	int16_t *out = malloc(bufn * sizeof *out);
	if (!in || !out) { free(in); free(out); pcm_close(&pcm); fclose(w.f); return die("out of memory"); }
	size_t filled = 0;      // output frames waiting in out
	size_t out_cap = bufn / 2, flush_at = pcm.period;
	int rc = 0;
	while (w.data_left >= w.align && !g_stop && !rc) {
		size_t want = chunk_frames * w.align;
		if (want > w.data_left) want = (size_t)w.data_left;
		size_t got = fread(in, 1, want - want % w.align, w.f);
		if (!got) break;
		w.data_left -= got;
		for (size_t off = 0; off + w.align <= got && !rc; off += w.align) {
			float l, rr;
			wav_frame(&w, in + off, &l, &rr);
			int16_t tmp[2 * CONV_MAX_FRAMES];
			unsigned n = conv_push(&cv, l, rr, tmp);
			for (unsigned i = 0; i < n; i++) { out[filled * 2] = tmp[i * 2]; out[filled * 2 + 1] = tmp[i * 2 + 1]; filled++; }
			if (filled >= flush_at || filled + CONV_MAX_FRAMES > out_cap) { rc = pcm_write(&pcm, out, filled); filled = 0; }
		}
	}
	if (!rc && !g_stop) {
		int16_t tail[2];
		unsigned fn = conv_flush(&cv, tail);
		if (fn && filled + fn <= out_cap) { out[filled * 2] = tail[0]; out[filled * 2 + 1] = tail[1]; filled += fn; }
	}
	if (!rc && filled) rc = pcm_write(&pcm, out, filled);
	if (!rc) rc = pcm_finish(&pcm);
	pcm_close(&pcm); fclose(w.f); free(in); free(out);
	return rc ? die("playback failed: %s", pcm_strerror(-rc)) : 0;
}

// ------------------------------------------------------------ status & mixer

static const char *volume_backend(const struct volume *v, char *buf, size_t n) {
	snprintf(buf, n, "cube-audio card %d %s", v->mx.card, v->label);
	return buf;
}

static int cmd_status(struct target t, const char *control, bool json) {
	struct volume v;
	char be[96];
	bool has_out = volume_bind_control(&v, t.card, false, control);
	int out_vol = has_out ? volume_get(&v) : -1;
	int out_mute = has_out ? volume_muted(&v) : -1;
	bool out_db = has_out ? v.db.ok : false;
	if (has_out) volume_backend(&v, be, sizeof be);
	else snprintf(be, sizeof be, "no volume control on card %d", t.card);
	if (has_out) mixer_close(&v.mx);

	struct volume iv;
	bool has_in = volume_bind_control(&iv, t.card, true, control);
	int in_vol = has_in ? volume_get(&iv) : -1;
	int in_mute = has_in ? volume_muted(&iv) : -1;
	if (has_in) mixer_close(&iv.mx);

	if (json) {
		printf("{\"device\":\"%d,%d\",\"volume\":%s", t.card, t.dev, has_out && out_vol >= 0 ? "" : "null");
		if (has_out && out_vol >= 0) printf("%d", out_vol);
		printf(",\"muted\":%s,\"backend\":", out_mute == 1 ? "true" : out_mute == 0 ? "false" : "null");
		json_print_str(be);
		printf(",\"db\":%s", has_out ? (out_db ? "true" : "false") : "false");
		if (has_in) {
			printf(",\"input_volume\":%d,\"input_muted\":%s", in_vol, in_mute == 1 ? "true" : "false");
		}
		printf("}\n");
		return 0;
	}
	printf("device=%d,%d\n", t.card, t.dev);
	if (has_out) {
		printf("volume=%d\nmuted=%s\nbackend=%s\n", out_vol, out_mute == 1 ? "yes" : "no", be);
		printf("db=%s\n", out_db ? "yes" : "no");
	} else {
		printf("volume=none\nmuted=no\nbackend=%s\n", be);
	}
	if (has_in) {
		printf("input_volume=%d\ninput_muted=%s\n", in_vol, in_mute == 1 ? "yes" : "no");
	}
	return 0;
}

static int cmd_volume(struct target t, bool input, const char *control, const char *arg, bool json) {
	struct volume v;
	if (!volume_bind_control(&v, t.card, input, control)) return die("card %d has no %s volume control", t.card, input ? "capture" : "playback");
	if (arg) {
		char *end;
		long n = strtol(arg, &end, 10);
		if (*end || end == arg || n < -100 || n > 100) { mixer_close(&v.mx); return die("volume must be 0-100, +N or -N"); }
		int cur = volume_get(&v);
		if (cur < 0) { mixer_close(&v.mx); return die("cannot read current volume"); }
		int want = (arg[0] == '+' || arg[0] == '-') ? cur + (int)n : (int)n;
		if (arg[0] != '+' && arg[0] != '-' && n < 0) { mixer_close(&v.mx); return die("volume must be 0-100"); }
		int r = volume_set(&v, clampi(want, 0, 100));
		if (r) { mixer_close(&v.mx); return die("cannot set volume: %s", strerror(-r)); }
	}
	int pct = volume_get(&v);
	if (pct < 0) { mixer_close(&v.mx); return die("cannot read volume"); }
	if (json) {
		printf("{\"card\":%d,\"input\":%s,\"control\":", t.card, input ? "true" : "false");
		json_print_str(v.label);
		printf(",\"volume\":%d}\n", pct);
	} else {
		printf("%d\n", pct);
	}
	mixer_close(&v.mx);
	return 0;
}

static int cmd_mute(struct target t, bool input, const char *control, const char *arg, bool json) {
	struct volume v;
	if (!volume_bind_control(&v, t.card, input, control)) return die("card %d has no %s volume control", t.card, input ? "capture" : "playback");
	int cur = volume_muted(&v);
	if (cur < 0) { mixer_close(&v.mx); return die("%s has no mute switch", v.label); }
	int want = cur;
	if (!arg) want = cur;                       // no argument only reports; toggling must be asked for
	else if (!strcmp(arg, "toggle")) want = !cur;
	else if (!strcmp(arg, "on")) want = 1;
	else if (!strcmp(arg, "off")) want = 0;
	else { mixer_close(&v.mx); return die("mute takes on, off or toggle"); }
	if (want != cur) {
		int r = volume_set_muted(&v, want);
		if (r) { mixer_close(&v.mx); return die("cannot change mute: %s", strerror(-r)); }
	}
	int now_muted = volume_muted(&v);
	if (json) {
		printf("{\"card\":%d,\"input\":%s,\"control\":", t.card, input ? "true" : "false");
		json_print_str(v.label);
		printf(",\"muted\":%s}\n", now_muted == 1 ? "true" : "false");
	} else {
		printf("%s\n", now_muted == 1 ? "muted" : "audible");
	}
	mixer_close(&v.mx);
	return 0;
}

// ----------------------------------------------------------------- selftest

static double now_s(void) { struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts); return ts.tv_sec + ts.tv_nsec / 1e9; }

static int check(bool ok, const char *what) { printf("  %-4s %s\n", ok ? "ok" : "FAIL", what); return ok ? 0 : 1; }

static int cmd_selftest(struct target t) {
	int bad = 0;
	printf("selftest on card %d device %d (plays 250 ms of silence)\n", t.card, t.dev);
	struct volume v;
	if (volume_bind(&v, t.card, false)) {
		int pct = volume_get(&v);
		bad += check(pct >= 0 && pct <= 100, "playback volume readable");
		bool roundtrip = true;
		for (int p = 0; p <= 100; p += 5) {
			int back = pct_from_raw(&v, raw_from_pct(&v, p));
			if (abs(back - p) > 3) roundtrip = false;
		}
		bad += check(roundtrip, "percent <-> raw mapping stable (no write)");
		int m = volume_muted(&v);
		bad += check(m == -1 || m == 0 || m == 1, "mute switch readable or absent");
		mixer_close(&v.mx);
	} else printf("  skip no playback volume control on this card\n");

	struct pcm pcm;
	int r = pcm_open_playback(&pcm, t, 48000, 2);
	if (bad += check(r == 0, "open playback, negotiate S16_LE stereo")) { printf("       %s\n", pcm_strerror(-r)); return 1; }
	printf("       rate %u Hz, period %lu frames, buffer %lu frames\n", pcm.rate, pcm.period, pcm.buffer);
	unsigned long total = pcm.rate / 4;
	int16_t *z = calloc(pcm.period * 2, sizeof *z);
	if (!z) { pcm_close(&pcm); return die("out of memory"); }
	double t0 = now_s();
	int wr = 0;
	for (unsigned long done = 0; done < total && !wr;) {
		unsigned long n = total - done < pcm.period ? total - done : pcm.period;
		wr = pcm_write(&pcm, z, n);
		done += n;
	}
	struct snd_pcm_status st;
	memset(&st, 0, sizeof st);
	bool st_ok = ioctl(pcm.fd, SNDRV_PCM_IOCTL_STATUS, &st) == 0;
	bad += check(wr == 0, "write silence");
	bad += check(st_ok && st.state == SNDRV_PCM_STATE_RUNNING, "stream started by start_threshold");
	int d = pcm_finish(&pcm);
	double dt = now_s() - t0;
	bad += check(d == 0, "drain completes");
	// 250 ms of audio must take ~250 ms to play out: proves the hardware clock is consuming it.
	bad += check(dt > 0.18 && dt < 0.60, "playout time tracks the hardware clock");
	printf("       elapsed %.0f ms\n", dt * 1000);
	free(z);
	pcm_close(&pcm);
	printf(bad ? "FAILED (%d)\n" : "all checks passed\n", bad);
	return bad ? 1 : 0;
}

// --------------------------------------------------------------------- main

#ifndef CUBE_AUDIO_NO_MAIN
static int usage(FILE *out) {
	fputs("usage: " PROG " [--help] [--version] cards [--json]\n"
	      "       " PROG " status [-D card,dev] [-c CTL] [--input] [--json]\n"
	      "       " PROG " volume [-D card,dev] [-c CTL] [--input] [--json] [N|+N|-N]\n"
	      "       " PROG " mute   [-D card,dev] [-c CTL] [--input] [--json] [on|off|toggle]\n"
	      "       " PROG " info   [--json] FILE.wav\n"
	      "       " PROG " play   [-D card,dev] FILE.wav | selftest [-D card,dev]\n", out);
	return out == stdout ? 0 : 2;
}

int main(int argc, char **argv) {
	struct target t = {0};
	bool have_t = false, input = false, json = false;
	const char *cmd = NULL, *control = NULL, *pos = NULL, *extra = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) return usage(stdout);
		if (!strcmp(argv[i], "-v") || !strcmp(argv[i], "--version")) {
			printf("%s %s\n", PROG, WHITECUBE_VERSION);
			return 0;
		}
		if (!strcmp(argv[i], "-D")) {
			if (i + 1 >= argc) return die("-D requires card,dev");
			if (!parse_target(argv[++i], &t)) return die("device must look like 0,0");
			have_t = true;
		} else if (!strcmp(argv[i], "-c") || !strcmp(argv[i], "--control")) {
			if (i + 1 >= argc) return die("%s requires a control name", argv[i]);
			control = argv[++i];
		} else if (!strcmp(argv[i], "--input")) input = true;
		else if (!strcmp(argv[i], "--json")) json = true;
		else if (!cmd) cmd = argv[i];
		else if (argv[i][0] == '-' && (!strcmp(cmd, "volume") ? !isdigit((unsigned char)argv[i][1]) : 1))
			return die("unknown option: %s", argv[i]);
		else if (!pos) pos = argv[i];
		else if (!extra) extra = argv[i];
		else return usage(stderr);
	}
	if (!cmd) return usage(stderr);
	if (!strcmp(cmd, "cards") && !pos) return cmd_cards(json);
	if (!strcmp(cmd, "info") && pos && !extra) return cmd_info(pos, json);
	if (!strcmp(cmd, "cards") || !strcmp(cmd, "info")) return usage(stderr);
	if (!have_t && !default_target(&t, input)) return die("no usable sound device (try '" PROG " cards')");
	if (!strcmp(cmd, "status") && !pos) return cmd_status(t, control, json);
	if (!strcmp(cmd, "volume") && !extra) return cmd_volume(t, input, control, pos, json);
	if (!strcmp(cmd, "mute") && !extra) return cmd_mute(t, input, control, pos, json);
	if (!strcmp(cmd, "selftest") && !pos) return cmd_selftest(t);
	if (!strcmp(cmd, "play") && pos && !extra) return cmd_play(t, pos);
	return usage(stderr);
}
#endif
