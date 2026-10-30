// cube-audio: Whitecube's own audio layer.
//
// Talks to the kernel sound drivers directly through their device nodes
// (/dev/snd/pcmC*D*p, /dev/snd/controlC*) with ioctls from the kernel UAPI header
// <sound/asound.h>. It links nothing but libc and libm: no alsa-lib, no PipeWire,
// no PulseAudio, no WirePlumber. The kernel driver (snd_hda_intel, snd-usb-audio...)
// is the only thing between this program and the hardware.
//
//   cube-audio cards                          list cards and playback/capture devices
//   cube-audio status                         key=value report (volume, mute, device)
//   cube-audio volume [--input] [N|+N|-N]     get/set volume in percent
//   cube-audio mute   [--input] [on|off|toggle]   (no argument: report only)
//   cube-audio play [-D card,dev] FILE.wav    play 8/16/24/32-bit PCM or float WAV
//   cube-audio selftest [-D card,dev]         probe hardware; plays 250 ms of silence
//
// Device choice: -D card,dev, else $CUBE_AUDIO_DEVICE, else the first card that has
// a playback volume control and a playback PCM.
#define _GNU_SOURCE
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
#define MAX_CARDS 32

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
	char path[48];
	snprintf(path, sizeof path, "/dev/snd/controlC%d", card);
	m->fd = open(path, O_RDWR | O_CLOEXEC);
	if (m->fd < 0) return -errno;
	m->card = card;
	struct snd_ctl_card_info ci;
	memset(&ci, 0, sizeof ci);
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_CARD_INFO, &ci) == 0) {
		snprintf(m->name, sizeof m->name, "%s", (char *)ci.id);
		snprintf(m->longname, sizeof m->longname, "%s", (char *)ci.name);
	}
	struct snd_ctl_elem_list l;
	memset(&l, 0, sizeof l);
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_LIST, &l) < 0) { int e = errno; close(m->fd); return -e; }
	if (l.count) {
		m->ids = calloc(l.count, sizeof *m->ids);
		if (!m->ids) { close(m->fd); return -ENOMEM; }
		l.space = l.count; l.pids = m->ids;
		if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_LIST, &l) < 0) { int e = errno; free(m->ids); close(m->fd); return -e; }
		m->n = l.used;
	}
	return 0;
}

static void mixer_close(struct mixer *m) { if (m->fd >= 0) close(m->fd); free(m->ids); m->ids = NULL; m->fd = -1; }

static bool mixer_find(const struct mixer *m, const char *name, struct ctl_elem *out) {
	for (unsigned i = 0; i < m->n; i++) {
		const struct snd_ctl_elem_id *id = &m->ids[i];
		if (id->iface != SNDRV_CTL_ELEM_IFACE_MIXER || strcmp((const char *)id->name, name)) continue;
		struct snd_ctl_elem_info inf;
		memset(&inf, 0, sizeof inf);
		inf.id.numid = id->numid;
		if (ioctl(m->fd, SNDRV_CTL_IOCTL_ELEM_INFO, &inf) < 0) continue;
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

static struct db_range elem_db(const struct mixer *m, const struct ctl_elem *e) {
	struct db_range d = {0};
	if (!(e->access & SNDRV_CTL_ELEM_ACCESS_TLV_READ)) return d;
	unsigned buf[2 + 64];
	struct snd_ctl_tlv *t = (struct snd_ctl_tlv *)buf;
	memset(buf, 0, sizeof buf);
	t->numid = e->id.numid; t->length = sizeof buf - sizeof *t;
	if (ioctl(m->fd, SNDRV_CTL_IOCTL_TLV_READ, t) < 0 || t->length < 16) return d;
	unsigned type = t->tlv[0];
	if (type == SNDRV_CTL_TLVT_DB_SCALE) {
		d = db_scale_range((int)t->tlv[2], (int)(t->tlv[3] & 0xffff), e->min, e->max);
	} else if (type == SNDRV_CTL_TLVT_DB_MINMAX || type == SNDRV_CTL_TLVT_DB_MINMAX_MUTE ||
	           type == SNDRV_CTL_TLVT_DB_LINEAR) {
		d.min = (int)t->tlv[2]; d.max = (int)t->tlv[3];
		d.ok = d.max > d.min && d.min >= -1000000 && d.max <= 1000000;
	}
	return d;
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

static bool volume_bind(struct volume *v, int card, bool input) {
	if (mixer_open(&v->mx, card) < 0) return false;
	const char *dir = input ? "Capture" : "Playback";
	for (const char *const *b = input ? IN_CONTROLS : OUT_CONTROLS; *b; b++) {
		char name[64];
		snprintf(name, sizeof name, "%s %s Volume", *b, dir);
		if (input && !strcmp(*b, "Capture")) snprintf(name, sizeof name, "Capture Volume");
		if (!mixer_find(&v->mx, name, &v->vol) || v->vol.type != SNDRV_CTL_ELEM_TYPE_INTEGER) continue;
		snprintf(v->label, sizeof v->label, "%s", *b);
		snprintf(name, sizeof name, "%s %s Switch", *b, dir);
		if (input && !strcmp(*b, "Capture")) snprintf(name, sizeof name, "Capture Switch");
		v->has_sw = mixer_find(&v->mx, name, &v->sw) && v->sw.type == SNDRV_CTL_ELEM_TYPE_BOOLEAN;
		v->db = elem_db(&v->mx, &v->vol);
		return true;
	}
	mixer_close(&v->mx);
	return false;
}

static int volume_get(const struct volume *v) {
	long vals[128] = {0};
	if (elem_read(&v->mx, &v->vol, vals) < 0) return -1;
	return pct_from_raw(v, vals[0]);
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

static int cmd_cards(void) {
	int found = 0;
	for (int c = 0; c < MAX_CARDS; c++) {
		struct mixer m;
		if (mixer_open(&m, c) < 0) continue;
		found++;
		printf("card %d: %s (%s)\n", c, m.name, m.longname);
		for (int pass = 0; pass < 2; pass++)
			for (int d = 0; d < 64; d++) {
				if (!pcm_exists(c, d, pass)) continue;
				struct snd_pcm_info pi;
				memset(&pi, 0, sizeof pi);
				pi.device = d; pi.stream = pass; pi.subdevice = 0;
				const char *name = ioctl(m.fd, SNDRV_CTL_IOCTL_PCM_INFO, &pi) == 0 ? (const char *)pi.name : "?";
				printf("  %s %d,%d: %s\n", pass ? "capture " : "playback", c, d, name);
			}
		mixer_close(&m);
	}
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
	m->bits[bit >> 5] = 1u << (bit & 31);
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
	char path[64];
	snprintf(path, sizeof path, "/dev/snd/pcmC%dD%dp", t.card, t.dev);
	pcm->fd = open(path, O_WRONLY | O_CLOEXEC);
	if (pcm->fd < 0) return -errno;
	int proto = 0;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_PVERSION, &proto) < 0 || SNDRV_PROTOCOL_MAJOR(proto) != SNDRV_PROTOCOL_MAJOR(SNDRV_PCM_VERSION)) {
		close(pcm->fd); return -EPROTO;
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
	if (err) { close(pcm->fd); return err; }
	pcm->channels = channels;
	// ~20 ms periods, four of them: low enough latency for a bell, safe against scheduling jitter.
	if ((err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, pcm->rate / 50)) ||
	    (err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_PERIODS, 4)) ||
	    (err = hw_pin(pcm->fd, &hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE, pi_(&hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min))) {
		close(pcm->fd); return err;
	}
	hp.rmask = ~0u;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_HW_PARAMS, &hp) < 0) { err = -errno; close(pcm->fd); return err; }
	pcm->period = pi_(&hp, SNDRV_PCM_HW_PARAM_PERIOD_SIZE)->min;
	pcm->buffer = pi_(&hp, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min;

	struct snd_pcm_sw_params sp;
	memset(&sp, 0, sizeof sp);
	sp.tstamp_mode = SNDRV_PCM_TSTAMP_NONE;
	sp.period_step = 1;
	sp.avail_min = pcm->period;
	sp.xfer_align = 1;
	sp.start_threshold = pcm->period * 2 < pcm->buffer ? pcm->period * 2 : pcm->buffer;
	sp.stop_threshold = pcm->buffer;       // an empty buffer is an underrun, reported as EPIPE
	sp.proto = SNDRV_PCM_VERSION;
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_SW_PARAMS, &sp) < 0) { err = -errno; close(pcm->fd); return err; }
	if (ioctl(pcm->fd, SNDRV_PCM_IOCTL_PREPARE) < 0) { err = -errno; close(pcm->fd); return err; }
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
			if (pos < 0 || (fstat(fileno(w->f), &st) == 0 && S_ISREG(st.st_mode) && pos + skip > st.st_size) ||
			    fseeko(w->f, skip, SEEK_CUR) != 0) {
				snprintf(err, errn, "%s: truncated", path); goto bad;
			}
		}
	}
	bool pcm_ok = w->fmt == 1 && (w->bits == 8 || w->bits == 16 || w->bits == 24 || w->bits == 32);
	bool flt_ok = w->fmt == 3 && w->bits == 32;
	if (!pcm_ok && !flt_ok) { snprintf(err, errn, "%s: unsupported format %u/%u-bit (need PCM 8/16/24/32 or float32)", path, w->fmt, w->bits); goto bad; }
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
		if (w->fmt == 3) { uint32_t u = le32(q); float f; memcpy(&f, &u, 4); s[c] = f; }
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

static int cmd_status(struct target t) {
	struct volume v;
	char be[96];
	printf("device=%d,%d\n", t.card, t.dev);
	if (volume_bind(&v, t.card, false)) {
		printf("volume=%d\nmuted=%s\nbackend=%s\n", volume_get(&v), volume_muted(&v) == 1 ? "yes" : "no", volume_backend(&v, be, sizeof be));
		printf("db=%s\n", v.db.ok ? "yes" : "no");
		mixer_close(&v.mx);
	} else {
		printf("volume=none\nmuted=no\nbackend=no volume control on card %d\n", t.card);
	}
	if (volume_bind(&v, t.card, true)) {
		printf("input_volume=%d\ninput_muted=%s\n", volume_get(&v), volume_muted(&v) == 1 ? "yes" : "no");
		mixer_close(&v.mx);
	}
	return 0;
}

static int cmd_volume(struct target t, bool input, const char *arg) {
	struct volume v;
	if (!volume_bind(&v, t.card, input)) return die("card %d has no %s volume control", t.card, input ? "capture" : "playback");
	if (arg) {
		char *end;
		long n = strtol(arg, &end, 10);
		if (*end || end == arg || n < -100 || n > 100) { mixer_close(&v.mx); return die("volume must be 0-100, +N or -N"); }
		int cur = volume_get(&v);
		int want = (arg[0] == '+' || arg[0] == '-') ? cur + (int)n : (int)n;
		if (arg[0] != '+' && arg[0] != '-' && n < 0) { mixer_close(&v.mx); return die("volume must be 0-100"); }
		int r = volume_set(&v, clampi(want, 0, 100));
		if (r) { mixer_close(&v.mx); return die("cannot set volume: %s", strerror(-r)); }
	}
	printf("%d\n", volume_get(&v));
	mixer_close(&v.mx);
	return 0;
}

static int cmd_mute(struct target t, bool input, const char *arg) {
	struct volume v;
	if (!volume_bind(&v, t.card, input)) return die("card %d has no %s volume control", t.card, input ? "capture" : "playback");
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
	printf("%s\n", volume_muted(&v) == 1 ? "muted" : "audible");
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
static int usage(void) {
	fputs("usage: " PROG " cards | status | volume [--input] [N|+N|-N] | mute [--input] [on|off|toggle]\n"
	      "       " PROG " play [-D card,dev] FILE.wav | selftest [-D card,dev]\n", stderr);
	return 2;
}

int main(int argc, char **argv) {
	if (argc < 2) return usage();
	const char *cmd = argv[1];
	struct target t = {0};
	bool have_t = false, input = false;
	const char *pos = NULL, *extra = NULL;
	for (int i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "-D") && i + 1 < argc) {
			if (!parse_target(argv[++i], &t)) return die("device must look like 0,0");
			have_t = true;
		} else if (!strcmp(argv[i], "--input")) input = true;
		else if (!pos) pos = argv[i];
		else if (!extra) extra = argv[i];
		else return usage();
	}
	if (!strcmp(cmd, "cards")) return cmd_cards();
	if (!have_t && !default_target(&t, input)) return die("no usable sound device (try '" PROG " cards')");
	if (!strcmp(cmd, "status")) return cmd_status(t);
	if (!strcmp(cmd, "volume")) return cmd_volume(t, input, pos);
	if (!strcmp(cmd, "mute")) return cmd_mute(t, input, pos);
	if (!strcmp(cmd, "selftest")) return cmd_selftest(t);
	if (!strcmp(cmd, "play") && pos && !extra) return cmd_play(t, pos);
	return usage();
}
#endif
