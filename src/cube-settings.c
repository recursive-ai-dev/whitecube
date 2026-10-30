// cube-settings: whitecube's OS-wide settings panel.
// Native Wayland (xdg-shell), cairo + pango on shm buffers. No toolkit or permanent bus;
// no portals. Reads and changes go through cube-settings-helper (root changes via
// sudo); displays use wlr-output-management; desktop settings are plain files the
// compositor reloads within a second, with typed per-user dconf overrides.
//
//   cube-settings                     open the panel
//   cube-settings --page NAME         open on a page (network, display, ...)
//   cube-settings --render NAME FILE  draw one page to a PNG without a window
#define _GNU_SOURCE
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/timerfd.h>
#include <sys/wait.h>
#include <cairo.h>
#include <pango/pangocairo.h>
#include <wayland-client.h>
#include <wayland-cursor.h>
#include <xkbcommon/xkbcommon.h>
#include <linux/input-event-codes.h>
#include "xdg-shell-client.h"
#include "wlr-output-management-client.h"
#include "status-theme.h"
#include "desktop-state.h"
#include "native-preferences.h"
#include "native-input.h"

#define HELPER "/usr/libexec/whitecube/cube-settings-helper"
#define SUDO "/usr/bin/sudo"
#define SUSPEND_COMMAND "/usr/bin/whitecube-suspend"
#define MAXF 10
#define MAXJOBS 24
#define MAXHITS 768
#define MAXHEADS 8
#define MAXMODES 96

static double S = 1.0; // UI scale from accessibility.json

// ------------------------------------------------------------------ utilities

static double now_s(void) {
	struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}
static void copy(char *dst, size_t cap, const char *src) { g_strlcpy(dst, src ? src : "", cap); }
static const char *home_dir(void) { const char *h = getenv("HOME"); return h && *h ? h : "/tmp"; }

// Fixed desktop commands launch asynchronously with an exec-error handshake.
static void spawn_detached(const char *cmd);

// ------------------------------------------------------- helper output tables

struct row { char *f[MAXF]; int n; };
struct table {
	char *data; struct row *rows; int n;
	bool loading, loaded; char err[256]; double stamp;
};
enum { T_NET, T_SCAN, T_POWER, T_SOUND, T_SYS, T_DISKS, T_USERS, T_UPDATES, T_ZONES,
	T_LAYOUTS, T_VARIANTS, T_SERVICES, T_FW, T_PRIVACY, T_COUNT };
static struct table tables[T_COUNT];

static void table_set(struct table *t, const char *out) {
	free(t->data); free(t->rows);
	t->data = strdup(out ? out : ""); t->rows = NULL; t->n = 0;
	int cap = 0;
	for (char *line = t->data, *next; line && *line; line = next) {
		next = strchr(line, '\n');
		if (next) *next++ = 0;
		if (!*line) continue;
		if (t->n == cap) { cap = cap ? cap * 2 : 64; t->rows = realloc(t->rows, cap * sizeof(*t->rows)); }
		struct row *r = &t->rows[t->n++];
		r->n = 0;
		for (char *f = line, *tab; f && r->n < MAXF; f = tab) {
			tab = strchr(f, '\t');
			if (tab) *tab++ = 0;
			r->f[r->n++] = f;
		}
		for (int i = r->n; i < MAXF; i++) r->f[i] = "";
	}
	t->loaded = true; t->err[0] = 0; t->stamp = now_s();
}
// Field 2 of the first row whose fields 0 and 1 match.
static const char *tv(int t, const char *type, const char *key) {
	for (int i = 0; i < tables[t].n; i++) {
		struct row *r = &tables[t].rows[i];
		if (!strcmp(r->f[0], type) && !strcmp(r->f[1], key)) return r->f[2];
	}
	return "";
}
static bool yes(const char *v) { return !strcmp(v, "yes"); }

// ------------------------------------------------------------------- jobs

enum priv { P_USER, P_FREE, P_PASS, P_PRIVACY, P_PREFS, P_SUSPEND, P_LAUNCH };
static char preferences_helper[PATH_MAX];
// User-only helper, separate from the privileged system helper and sudo policy.
static char privacy_helper[PATH_MAX];
static void find_privacy_helper(void) {
	char executable[PATH_MAX - 32];
	ssize_t n = readlink("/proc/self/exe", executable, sizeof(executable) - 1);
	if (n > 0) {
		executable[n] = 0;
		char *slash = strrchr(executable, '/');
		if (slash) {
			*slash = 0;
			snprintf(privacy_helper, sizeof(privacy_helper), "%s/cube-privacy", executable);
			if (access(privacy_helper, X_OK) == 0) return;
			// Development binaries live in build/ alongside the source directory.
			if (!strcmp(slash + 1, "cube-settings") && (slash = strrchr(executable, '/')) && !strcmp(slash + 1, "build")) {
				*slash = 0;
				snprintf(privacy_helper, sizeof(privacy_helper), "%s/cube-privacy", executable);
				if (access(privacy_helper, X_OK) == 0) return;
			}
		}
	}
	snprintf(privacy_helper, sizeof(privacy_helper), "%s/.local/bin/cube-privacy", home_dir());
}
enum chain { C_NONE, C_CONNECT_ADDED };
#define MAX_JOB_OUT (4 * 1024 * 1024)
struct job {
	bool used, reaped, timed_out; pid_t pid; int status; int out, err; int table; enum priv priv; enum chain chain;
	double deadline;
	char *obuf; size_t olen, ocap; char ebuf[2048]; size_t elen; char label[96];
};
static struct job jobs[MAXJOBS];
static char g_password[256];
static bool g_have_password;

struct pending {
	bool used; char label[96]; enum priv priv; enum chain chain;
	char args[6][256]; int nargs; char input[12288];
};

static void toast(const char *fmt, ...);
static void request_redraw(void);
static void refresh_page(bool force);
static void cfg_load(void);
static void job_start_pending(struct pending *p);

static bool job_running(int table) {
	for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used && jobs[i].table == table) return true;
	return false;
}
static bool any_action_running(void) {
	for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used && jobs[i].table < 0) return true;
	return false;
}

static bool job_launch(int table, const char *label, enum priv priv, enum chain chain, const char *input,
		       int nargs, char args[][256]) {
	int slot = -1;
	for (int i = 0; i < MAXJOBS; i++) if (!jobs[i].used) { slot = i; break; }
	if (slot < 0) return false;
	int in[2] = {-1, -1}, out[2] = {-1, -1}, err[2] = {-1, -1};
	if (pipe2(in, O_CLOEXEC) || pipe2(out, O_CLOEXEC) || pipe2(err, O_CLOEXEC)) {
		for (int i = 0; i < 2; i++) { if (in[i] >= 0) close(in[i]); if (out[i] >= 0) close(out[i]); if (err[i] >= 0) close(err[i]); }
		return false;
	}
	const char *argv[16]; int a = 0;
	if (priv == P_FREE) { argv[a++] = SUDO; argv[a++] = "-n"; argv[a++] = "--"; }
	if (priv == P_PASS) { argv[a++] = SUDO; argv[a++] = "-S"; argv[a++] = "-p"; argv[a++] = ""; argv[a++] = "--"; }
	argv[a++] = priv == P_SUSPEND ? SUSPEND_COMMAND : priv == P_PREFS ? preferences_helper : priv == P_PRIVACY ? privacy_helper : HELPER;
	for (int i = 0; i < nargs && a < 15; i++) argv[a++] = args[i];
	argv[a] = NULL;
	pid_t pid = fork();
	if (pid < 0) {
		for (int i = 0; i < 2; i++) { close(in[i]); close(out[i]); close(err[i]); }
		return false;
	}
	if (pid == 0) {
		setsid();
		sigset_t empty; sigemptyset(&empty); sigprocmask(SIG_SETMASK, &empty, NULL);
		dup2(in[0], 0); dup2(out[1], 1); dup2(err[1], 2);
		signal(SIGCHLD, SIG_DFL);
		signal(SIGPIPE, SIG_DFL);
		execv(argv[0], (char *const *)argv);
		fprintf(stderr, "cannot run %s: %s\n", argv[0], strerror(errno));
		_exit(127);
	}
	close(in[0]); close(out[1]); close(err[1]);
	char feed[12800]; size_t len = 0;
	if (priv == P_PASS) {
		int k = snprintf(feed, sizeof(feed), "%s\n", g_password);
		if (k > 0) len = (size_t)k < sizeof(feed) ? (size_t)k : sizeof(feed) - 1;
	}
	if (input && len + 1 < sizeof(feed)) {
		int k = snprintf(feed + len, sizeof(feed) - len, "%s", input);
		if (k > 0) {
			size_t add = (size_t)k < sizeof(feed) - len ? (size_t)k : sizeof(feed) - len - 1;
			len += add;
		}
	}
	for (size_t off = 0; off < len; ) {
		ssize_t w = write(in[1], feed + off, len - off);
		if (w > 0) off += (size_t)w;
		else if (w < 0 && errno == EINTR) continue;
		else break;
	}
	explicit_bzero(feed, sizeof(feed));
	if (priv == P_PASS) { explicit_bzero(g_password, sizeof(g_password)); g_have_password = false; }
	close(in[1]);
	fcntl(out[0], F_SETFL, O_NONBLOCK); fcntl(err[0], F_SETFL, O_NONBLOCK);
	struct job *j = &jobs[slot];
	memset(j, 0, sizeof(*j));
	j->used = true; j->pid = pid; j->out = out[0]; j->err = err[0]; j->table = table; j->priv = priv; j->chain = chain;
	j->deadline = now_s() + (table >= 0 ? 35 : 65);
	copy(j->label, sizeof(j->label), label);
	if (table >= 0) tables[table].loading = true;
	request_redraw();
	return true;
}

// Load a helper read into a table (no-op while one is already running).
static void load(int table, enum priv priv, int nargs, ...) {
	if (job_running(table)) return;
	char args[6][256] = {{0}};
	va_list ap; va_start(ap, nargs);
	for (int i = 0; i < nargs && i < 6; i++) copy(args[i], 256, va_arg(ap, const char *));
	va_end(ap);
	job_launch(table, "", priv, C_NONE, NULL, nargs, args);
}

// Modal dialogs: password prompt, confirmation, and two-field input.
enum modal_kind { M_NONE, M_PASSWORD, M_CONFIRM, M_ADD_WIFI };
struct field { char buf[256]; };
static struct {
	enum modal_kind kind; char title[128], text[256], ok_label[32];
	struct pending pending; struct field a, b; bool danger;
} modal;

static void run_pending(struct pending *p) {
	if (p->priv == P_PASS && !g_have_password) {
		modal.kind = M_PASSWORD; modal.pending = *p; modal.a.buf[0] = 0;
		copy(modal.title, sizeof(modal.title), "Password required");
		snprintf(modal.text, sizeof(modal.text), "%s needs administrator rights. Enter your password.", p->label);
		copy(modal.ok_label, sizeof(modal.ok_label), "Continue");
		request_redraw();
		return;
	}
	job_start_pending(p);
}
static void job_start_pending(struct pending *p) {
	if (!job_launch(-1, p->label, p->priv, p->chain, p->input[0] ? p->input : NULL, p->nargs, p->args))
		toast("Too many tasks running; try again");
	explicit_bzero(p->input, sizeof(p->input));
}
static void action(const char *label, enum priv priv, int nargs, ...) {
	struct pending p = {0};
	p.used = true; p.priv = priv; copy(p.label, sizeof(p.label), label);
	va_list ap; va_start(ap, nargs);
	for (int i = 0; i < nargs && i < 6; i++) copy(p.args[i], 256, va_arg(ap, const char *));
	va_end(ap);
	p.nargs = nargs;
	run_pending(&p);
}
static void confirm(const char *title, const char *text, const char *ok, bool danger, struct pending *p) {
	modal.kind = M_CONFIRM; modal.pending = *p; modal.danger = danger;
	copy(modal.title, sizeof(modal.title), title); copy(modal.text, sizeof(modal.text), text);
	copy(modal.ok_label, sizeof(modal.ok_label), ok);
	request_redraw();
}
static struct pending make_pending(const char *label, enum priv priv, int nargs, ...) {
	struct pending p = {0};
	p.used = true; p.priv = priv; copy(p.label, sizeof(p.label), label);
	va_list ap; va_start(ap, nargs);
	for (int i = 0; i < nargs && i < 6; i++) copy(p.args[i], 256, va_arg(ap, const char *));
	va_end(ap);
	p.nargs = nargs;
	return p;
}

static const char *last_line(const char *s, char *buf, size_t cap) {
	buf[0] = 0;
	const char *end = s + strlen(s);
	while (end > s && (end[-1] == '\n' || end[-1] == ' ')) end--;
	const char *start = end;
	while (start > s && start[-1] != '\n') start--;
	snprintf(buf, cap, "%.*s", (int)(end - start), start);
	return buf;
}
static bool sudo_auth_failed(const char *err) {
	return strstr(err, "incorrect password") || strstr(err, "Sorry, try again") ||
	       strstr(err, "no password was provided") || strstr(err, "a password is required");
}

static void job_finish(struct job *j, int status) {
	if (j->obuf) j->obuf[j->olen] = 0;
	if (j->timed_out && !j->elen) j->elen = (size_t)snprintf(j->ebuf, sizeof(j->ebuf), "timed out");
	j->ebuf[j->elen] = 0;
	bool ok = !j->timed_out && WIFEXITED(status) && WEXITSTATUS(status) == 0;
	char line[256];
	if (j->priv == P_LAUNCH) {
		if (!ok || j->elen) toast("Cannot launch %s: %s", j->label, last_line(j->ebuf, line, sizeof(line))[0] ? line : "process creation failed");
		free(j->obuf); memset(j, 0, sizeof(*j)); request_redraw(); return;
	}
	if (j->table >= 0) {
		struct table *t = &tables[j->table];
		t->loading = false;
		if (ok) table_set(t, j->obuf ? j->obuf : "");
		else { copy(t->err, sizeof(t->err), last_line(j->ebuf, line, sizeof(line))); t->loaded = true; }
	} else if (ok) {
		if (j->chain == C_CONNECT_ADDED && j->obuf) {
			char id[32] = "";
			const char *p = strstr(j->obuf, "ok\tnet-add\t");
			if (p && sscanf(p + 11, "%16[0-9a-f]", id) == 1) {
				struct pending c = make_pending("Connect", P_FREE, 2, "net-connect", id);
				job_start_pending(&c);
			}
		}
		cfg_load();
		toast(j->priv == P_PREFS ? "%s: saved and verified" : "%s: done", j->label);
		refresh_page(true);
	} else {
		if (j->priv == P_PASS && sudo_auth_failed(j->ebuf)) {
			explicit_bzero(g_password, sizeof(g_password)); g_have_password = false;
			toast("Wrong password; %s was not changed", j->label);
		} else toast("%s failed: %s", j->label, last_line(j->ebuf, line, sizeof(line))[0] ? line : "unknown error");
		refresh_page(true);
	}
	free(j->obuf);
	memset(j, 0, sizeof(*j));
	request_redraw();
}

static void job_reap(struct job *j) {
	if (j->reaped || j->pid <= 0) return;
	int status = 0;
	pid_t r = waitpid(j->pid, &status, WNOHANG);
	if (r == j->pid) { j->reaped = true; j->status = status; }
	else if (r < 0 && errno != EINTR) { j->reaped = true; j->status = 1; }
}

static void job_read(struct job *j) {
	char buf[4096];
	for (;;) {
		bool progress = false;
		if (j->out >= 0) {
			ssize_t n = read(j->out, buf, sizeof(buf));
			if (n > 0) {
				size_t room = j->olen < MAX_JOB_OUT ? MAX_JOB_OUT - j->olen : 0;
				size_t take = (size_t)n < room ? (size_t)n : room;
				if (take > 0) {
					if (j->olen + take + 1 > j->ocap) {
						size_t ncap = (j->olen + take + 1) * 2;
						if (ncap > MAX_JOB_OUT + 1) ncap = MAX_JOB_OUT + 1;
						char *nb = realloc(j->obuf, ncap);
						if (nb) { j->obuf = nb; j->ocap = ncap; }
						else take = 0;
					}
					if (take > 0 && j->obuf) { memcpy(j->obuf + j->olen, buf, take); j->olen += take; }
				}
				progress = true;
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) { close(j->out); j->out = -1; }
		}
		if (j->err >= 0) {
			ssize_t n = read(j->err, buf, sizeof(buf));
			if (n > 0) {
				size_t room = sizeof(j->ebuf) - 1 - j->elen;
				if ((size_t)n > room) { // keep the tail: the reason is on the last line
					size_t drop = (size_t)n - room;
					if (drop > j->elen) drop = j->elen;
					memmove(j->ebuf, j->ebuf + drop, j->elen - drop); j->elen -= drop;
					room = sizeof(j->ebuf) - 1 - j->elen;
				}
				size_t take = (size_t)n < room ? (size_t)n : room;
				memcpy(j->ebuf + j->elen, buf + n - take, take); j->elen += take; progress = true;
			} else if (n == 0 || (errno != EAGAIN && errno != EINTR)) { close(j->err); j->err = -1; }
		}
		if (!progress) break;
	}
	job_reap(j);
	if (!j->timed_out && j->deadline > 0 && now_s() >= j->deadline) {
		j->timed_out = true;
		if (!j->reaped && j->pid > 0) {
			if (j->priv != P_LAUNCH) kill(-j->pid, SIGKILL);
			kill(j->pid, SIGKILL);
		}
		if (j->out >= 0) { close(j->out); j->out = -1; }
		if (j->err >= 0) { close(j->err); j->err = -1; }
	}
	if (j->out < 0 && j->err < 0) {
		for (int spin = 0; !j->reaped && spin < 10; spin++) {
			job_reap(j);
			if (!j->reaped) usleep(200);
		}
		if (j->reaped) job_finish(j, j->status);
	}
}

// --------------------------------------------------------------- config files

#define MAXCFG 96
struct kv { char key[48]; char val[256]; };
static struct kv cfg_user[MAXCFG], cfg_sys[MAXCFG];
static int ncfg_user, ncfg_sys;
static struct kv cfg_prefs[MAXCFG];
static int ncfg_prefs;
static void cfg_preference(const char *key, const char *value, void *unused) {
	if (ncfg_prefs >= MAXCFG) return;
	copy(cfg_prefs[ncfg_prefs].key, sizeof(cfg_prefs[0].key), key);
	copy(cfg_prefs[ncfg_prefs++].val, sizeof(cfg_prefs[0].val), value);
}

static void user_config_path(char *buf, size_t cap) { snprintf(buf, cap, "%s/.config/whitecube/config", home_dir()); }
static void cfg_read(const char *path, struct kv *out, int *n) {
	*n = 0;
	FILE *f = fopen(path, "r");
	if (!f) return;
	char *line = NULL; size_t capacity = 0;
	while (getline(&line, &capacity, f) >= 0) {
		if (line[0] == '#' || !strchr(line, '=')) continue;
		line[strcspn(line, "\r\n")] = 0;
		char *eq = strchr(line, '='); *eq = 0;
		if (strlen(line) >= sizeof(out[0].key)) continue;
		int slot = 0;
		while (slot < *n && strcmp(out[slot].key, line)) slot++;
		if (slot >= MAXCFG) continue;
		copy(out[slot].key, sizeof(out[slot].key), line);
		copy(out[slot].val, sizeof(out[slot].val), eq + 1);
		if (slot == *n) (*n)++;
	}
	free(line); fclose(f);
}
static void cfg_load(void) {
	char path[512]; user_config_path(path, sizeof(path));
	cfg_read(path, cfg_user, &ncfg_user);
	cfg_read("/etc/whitecube/config", cfg_sys, &ncfg_sys);
	ncfg_prefs = 0; wc_prefs_visit(cfg_preference, NULL);
}
static const char *cfg_get(const char *key, const char *fallback) {
	for (int i = 0; i < ncfg_prefs; i++) if (!strcmp(cfg_prefs[i].key, key)) return cfg_prefs[i].val;
	for (int i = ncfg_user - 1; i >= 0; i--) if (!strcmp(cfg_user[i].key, key)) return cfg_user[i].val;
	for (int i = ncfg_sys - 1; i >= 0; i--) if (!strcmp(cfg_sys[i].key, key)) return cfg_sys[i].val;
	return fallback;
}
static const char *cfg_boolean(const char *key, const char *fallback) {
	const char *v = cfg_get(key, fallback);
	if (!strcmp(v, "true") || !strcmp(v, "1")) return "1";
	if (!strcmp(v, "false") || !strcmp(v, "0")) return "0";
	return fallback;
}
// Replace or append keys in the user config, keeping comments and other lines.
// Atomic rename, so whitecube's mtime check sees one complete file.
static bool cfg_set_many(int n, const char *const keys[], const char *const vals[]) {
	if (n < 1 || n > 32) return false;
	bool native = true;
	for (int i = 0; i < n; i++) {
		if (!keys[i] || !vals[i] || strpbrk(keys[i], "\r\n\t=") || strpbrk(vals[i], "\r\n\t")) {
			toast("Invalid setting value"); return false;
		}
		native &= wc_pref_known(keys[i]);
	}
	if (native) {
		if (any_action_running()) { toast("Wait for the current change to finish"); return false; }
		struct pending pending = make_pending("Preferences", P_PREFS, 1, "set-many");
		size_t used = 0;
		for (int i = 0; i < n; i++) {
			int len = snprintf(pending.input + used, sizeof(pending.input) - used, "%s\t%s\n", keys[i], vals[i]);
			if (len < 0 || (size_t)len >= sizeof(pending.input) - used) { toast("Settings are too long"); return false; }
			used += len;
		}
		bool launched = job_launch(-1, "Preferences", P_PREFS, C_NONE, pending.input, pending.nargs, pending.args);
		toast(launched ? "Saving preferences…" : "Cannot start settings writer");
		return launched;
	}
	char path[512], tmp[540], dir[512];
	user_config_path(path, sizeof(path));
	snprintf(dir, sizeof(dir), "%s/.config", home_dir());
	if (mkdir(dir, 0700) && errno != EEXIST) { toast("Cannot create settings directory"); return false; }
	snprintf(dir, sizeof(dir), "%s/.config/whitecube", home_dir());
	if (mkdir(dir, 0700) && errno != EEXIST) { toast("Cannot create Whitecube settings directory"); return false; }
	snprintf(tmp, sizeof(tmp), "%s.tmp-XXXXXX", path);
	FILE *in = fopen(path, "r");
	if (!in && errno != ENOENT) { toast("Cannot read settings; changes were not saved"); return false; }
	int fd = mkstemp(tmp);
	FILE *out = fd >= 0 ? fdopen(fd, "w") : NULL;
	if (!out) { if (in) fclose(in); if (fd >= 0) { close(fd); unlink(tmp); } toast("Cannot save settings"); return false; }
	bool done[32] = {0}, ended = true;
	char *line = NULL; size_t cap = 0; ssize_t len;
	while (in && (len = getline(&line, &cap, in)) >= 0) {
		bool replaced = false;
		for (int i = 0; i < n; i++) {
			size_t kl = strlen(keys[i]);
			if (!strncmp(line, keys[i], kl) && line[kl] == '=') {
				if (!done[i]) { fprintf(out, "%s=%s\n", keys[i], vals[i]); ended = true; }
				done[i] = replaced = true;
			}
		}
		if (!replaced) { fputs(line, out); ended = len > 0 && line[len - 1] == '\n'; }
	}
	bool ok = !in || !ferror(in);
	free(line);
	for (int i = 0; i < n; i++) if (!done[i]) {
		if (!ended) fputc('\n', out);
		fprintf(out, "%s=%s\n", keys[i], vals[i]); ended = true;
	}
	if (in) fclose(in);
	ok = ok && !ferror(out) && fflush(out) == 0 && fsync(fileno(out)) == 0;
	ok &= fclose(out) == 0;
	if (!ok || rename(tmp, path)) { unlink(tmp); toast("Saving settings failed"); return false; }
	cfg_load(); request_redraw();
	return true;
}
static bool cfg_set(const char *key, const char *val) {
	const char *k[1] = {key}, *v[1] = {val};
	return cfg_set_many(1, k, v);
}

// accessibility.json, shared with whitecube-desktop and the compositor.
static struct { int ui_scale, terminal_scale; bool strong, reduced_motion; } acc = {100, 100, false, false};
static void state_path(char *buf, size_t cap, const char *name) {
	const char *state = getenv("XDG_STATE_HOME");
	if (state && *state) snprintf(buf, cap, "%s/whitecube/%s", state, name);
	else snprintf(buf, cap, "%s/.local/state/whitecube/%s", home_dir(), name);
}
static void acc_load(void) {
	char path[512]; state_path(path, sizeof(path), "accessibility.json");
	struct wc_accessibility settings = {1, 1, false, false};
	wc_accessibility_load(path, &settings);
	acc.ui_scale = (int)lround(settings.ui_scale * 100);
	acc.terminal_scale = (int)lround(settings.terminal_scale * 100);
	acc.strong = settings.strong; acc.reduced_motion = settings.reduced_motion;
}
static bool acc_save(void) {
	char path[512], tmp[540], dir[512];
	if (!wc_state_directory(dir, sizeof(dir))) { toast("Cannot create accessibility directory"); acc_load(); return false; }
	state_path(path, sizeof(path), "accessibility.json");
	snprintf(tmp, sizeof(tmp), "%s.tmp-XXXXXX", path);
	int fd = mkstemp(tmp);
	if (fd < 0) { toast("Cannot save accessibility settings"); acc_load(); return false; }
	char data[256];
	int n = snprintf(data, sizeof(data), "{\"ui_scale\": %.2f, \"terminal_scale\": %.2f, \"contrast\": \"%s\", \"reduced_motion\": %s}\n",
		acc.ui_scale / 100.0, acc.terminal_scale / 100.0, acc.strong ? "strong" : "normal", acc.reduced_motion ? "true" : "false");
	bool ok = write(fd, data, n) == n && fsync(fd) == 0;
	ok &= close(fd) == 0;
	if (!ok || rename(tmp, path)) { unlink(tmp); toast("Cannot save accessibility settings"); acc_load(); return false; }
	acc_load(); toast("Accessibility saved"); return true;
}

// --------------------------------------------------- wlr-output-management

struct omode { struct zwlr_output_mode_v1 *m; int w, h, refresh; bool preferred, live; };
struct ohead {
	struct zwlr_output_head_v1 *h; bool live;
	char name[64], desc[160], make[64], model[64];
	int pw, ph; bool enabled; int cur; int x, y, transform; double scale;
	struct omode modes[MAXMODES]; int nmodes;
	bool p_enabled; int p_mode, p_transform; double p_scale; struct field fx, fy; bool edited;
};
static struct {
	struct zwlr_output_manager_v1 *mgr; uint32_t serial; bool have_serial;
	struct ohead heads[MAXHEADS]; int n;
	struct ohead submitted[MAXHEADS]; int submitted_n;
	struct zwlr_output_configuration_v1 *pending_config;
} om;

static struct ohead *head_of(struct zwlr_output_head_v1 *h) {
	for (int i = 0; i < om.n; i++) if (om.heads[i].h == h) return &om.heads[i];
	return NULL;
}
static void head_reset_pending(struct ohead *hd) {
	hd->p_enabled = hd->enabled; hd->p_mode = hd->cur; hd->p_transform = hd->transform; hd->p_scale = hd->scale;
	snprintf(hd->fx.buf, sizeof(hd->fx.buf), "%d", hd->x); snprintf(hd->fy.buf, sizeof(hd->fy.buf), "%d", hd->y);
	hd->edited = false;
}
static void mode_size(void *d, struct zwlr_output_mode_v1 *m, int32_t w, int32_t h) {
	struct omode *mo = d; mo->w = w; mo->h = h;
}
static void mode_refresh(void *d, struct zwlr_output_mode_v1 *m, int32_t r) { ((struct omode *)d)->refresh = r; }
static void mode_preferred(void *d, struct zwlr_output_mode_v1 *m) { ((struct omode *)d)->preferred = true; }
static void mode_finished(void *d, struct zwlr_output_mode_v1 *m) {
	struct omode *mo = d; mo->live = false; zwlr_output_mode_v1_destroy(m); mo->m = NULL;
}
static const struct zwlr_output_mode_v1_listener mode_listener = { mode_size, mode_refresh, mode_preferred, mode_finished };

static void head_name(void *d, struct zwlr_output_head_v1 *h, const char *s) { struct ohead *hd = head_of(h); if (hd) copy(hd->name, sizeof(hd->name), s); }
static void head_desc(void *d, struct zwlr_output_head_v1 *h, const char *s) { struct ohead *hd = head_of(h); if (hd) copy(hd->desc, sizeof(hd->desc), s); }
static void head_phys(void *d, struct zwlr_output_head_v1 *h, int32_t w, int32_t ht) { struct ohead *hd = head_of(h); if (hd) { hd->pw = w; hd->ph = ht; } }
static void head_mode(void *d, struct zwlr_output_head_v1 *h, struct zwlr_output_mode_v1 *m) {
	struct ohead *hd = head_of(h);
	if (!hd || hd->nmodes >= MAXMODES) { zwlr_output_mode_v1_destroy(m); return; }
	struct omode *mo = &hd->modes[hd->nmodes++];
	memset(mo, 0, sizeof(*mo)); mo->m = m; mo->live = true;
	zwlr_output_mode_v1_add_listener(m, &mode_listener, mo);
}
static void head_enabled(void *d, struct zwlr_output_head_v1 *h, int32_t e) { struct ohead *hd = head_of(h); if (hd) hd->enabled = e; }
static void head_current_mode(void *d, struct zwlr_output_head_v1 *h, struct zwlr_output_mode_v1 *m) {
	struct ohead *hd = head_of(h);
	if (!hd) return;
	for (int i = 0; i < hd->nmodes; i++) if (hd->modes[i].m == m) hd->cur = i;
}
static void head_position(void *d, struct zwlr_output_head_v1 *h, int32_t x, int32_t y) { struct ohead *hd = head_of(h); if (hd) { hd->x = x; hd->y = y; } }
static void head_transform(void *d, struct zwlr_output_head_v1 *h, int32_t t) { struct ohead *hd = head_of(h); if (hd) hd->transform = t; }
static void head_scale(void *d, struct zwlr_output_head_v1 *h, wl_fixed_t s) { struct ohead *hd = head_of(h); if (hd) hd->scale = wl_fixed_to_double(s); }
static void head_finished(void *d, struct zwlr_output_head_v1 *h) {
	struct ohead *hd = head_of(h);
	if (!hd) return;
	for (int i = 0; i < hd->nmodes; i++) if (hd->modes[i].m) zwlr_output_mode_v1_destroy(hd->modes[i].m);
	zwlr_output_head_v1_destroy(h);
	int idx = (int)(hd - om.heads);
	memmove(&om.heads[idx], &om.heads[idx + 1], (om.n - idx - 1) * sizeof(om.heads[0]));
	om.n--;
	// Mode listeners point into the moved array; re-point them.
	for (int i = idx; i < om.n; i++)
		for (int k = 0; k < om.heads[i].nmodes; k++)
			if (om.heads[i].modes[k].m) wl_proxy_set_user_data((struct wl_proxy *)om.heads[i].modes[k].m, &om.heads[i].modes[k]);
	request_redraw();
}
static void head_make(void *d, struct zwlr_output_head_v1 *h, const char *s) { struct ohead *hd = head_of(h); if (hd) copy(hd->make, sizeof(hd->make), s); }
static void head_model(void *d, struct zwlr_output_head_v1 *h, const char *s) { struct ohead *hd = head_of(h); if (hd) copy(hd->model, sizeof(hd->model), s); }
static void head_serial(void *d, struct zwlr_output_head_v1 *h, const char *s) {}
static void head_adaptive(void *d, struct zwlr_output_head_v1 *h, uint32_t s) {}
static const struct zwlr_output_head_v1_listener head_listener = {
	head_name, head_desc, head_phys, head_mode, head_enabled, head_current_mode, head_position,
	head_transform, head_scale, head_finished, head_make, head_model, head_serial, head_adaptive,
};
static void mgr_head(void *d, struct zwlr_output_manager_v1 *m, struct zwlr_output_head_v1 *h) {
	if (om.n >= MAXHEADS) { zwlr_output_head_v1_destroy(h); return; }
	struct ohead *hd = &om.heads[om.n++];
	memset(hd, 0, sizeof(*hd)); hd->h = h; hd->live = true; hd->scale = 1;
	zwlr_output_head_v1_add_listener(h, &head_listener, NULL);
}
static void mgr_done(void *d, struct zwlr_output_manager_v1 *m, uint32_t serial) {
	om.serial = serial; om.have_serial = true;
	for (int i = 0; i < om.n; i++) if (!om.heads[i].edited) head_reset_pending(&om.heads[i]);
	request_redraw();
}
static void mgr_finished(void *d, struct zwlr_output_manager_v1 *m) { om.mgr = NULL; }
static const struct zwlr_output_manager_v1_listener mgr_listener = { mgr_head, mgr_done, mgr_finished };

static bool output_coordinate(const char *text, int *result) {
	if (!text || !*text) return false;
	char *end; errno = 0; long value = strtol(text, &end, 10);
	if (errno || end == text || *end || value < -32768 || value > 32767) return false;
	*result = (int)value; return true;
}
static bool outputs_valid(void) {
	bool any = false;
	for (int i = 0; i < om.n; i++) {
		struct ohead *hd = &om.heads[i]; if (!hd->live) continue;
		any |= hd->p_enabled;
		if (!hd->p_enabled) continue;
		int x, y;
		if (!output_coordinate(hd->fx.buf, &x) || !output_coordinate(hd->fy.buf, &y)) {
			toast("Display positions must be whole numbers from -32768 to 32767"); return false;
		}
		if (!isfinite(hd->p_scale) || hd->p_scale < .5 || hd->p_scale > 3 || hd->p_transform < 0 || hd->p_transform > 7) {
			toast("Invalid display scale or rotation"); return false;
		}
		if (hd->p_mode >= 0 && (hd->p_mode >= hd->nmodes || !hd->modes[hd->p_mode].live)) {
			toast("That display mode is no longer available"); return false;
		}
	}
	if (!any) toast("At least one screen must stay on");
	return any;
}
static bool save_outputs_file(void) {
	char path[512], tmp[540], directory[512];
	snprintf(directory, sizeof(directory), "%s/.config/whitecube", home_dir());
	if (g_mkdir_with_parents(directory, 0700)) return false;
	snprintf(path, sizeof(path), "%s/.config/whitecube/outputs", home_dir());
	snprintf(tmp, sizeof(tmp), "%s.tmp-XXXXXX", path);
	int fd = mkstemp(tmp); FILE *f = fd >= 0 ? fdopen(fd, "w") : NULL;
	if (!f) { if (fd >= 0) { close(fd); unlink(tmp); } return false; }
	fprintf(f, "# Written by cube-settings. NAME enabled= mode=WxH@mHz scale= transform= x= y=\n");
	for (int i = 0; i < om.submitted_n; i++) {
		struct ohead *hd = &om.submitted[i]; if (!hd->live) continue;
		struct omode *m = hd->p_mode >= 0 && hd->p_mode < hd->nmodes ? &hd->modes[hd->p_mode] : NULL;
		fprintf(f, "%s enabled=%d", hd->name, hd->p_enabled);
		if (m) fprintf(f, " mode=%dx%d@%d", m->w, m->h, m->refresh);
		fprintf(f, " scale=%.6f transform=%d x=%d y=%d\n", hd->p_scale, hd->p_transform, atoi(hd->fx.buf), atoi(hd->fy.buf));
	}
	bool ok = !ferror(f) && fflush(f) == 0 && fsync(fileno(f)) == 0;
	ok &= fclose(f) == 0;
	if (!ok || rename(tmp, path)) { unlink(tmp); return false; }
	return true;
}
static void config_succeeded(void *d, struct zwlr_output_configuration_v1 *c) {
	bool saved = save_outputs_file();
	zwlr_output_configuration_v1_destroy(c); om.pending_config = NULL;
	for (int i = 0; i < om.n; i++) head_reset_pending(&om.heads[i]);
	toast(saved ? "Display settings applied and saved" : "Display applied, but saving failed; it will not persist after login");
}
static void config_failed(void *d, struct zwlr_output_configuration_v1 *c) {
	zwlr_output_configuration_v1_destroy(c); om.pending_config = NULL;
	toast("The screen rejected those settings; nothing was changed");
}
static void config_cancelled(void *d, struct zwlr_output_configuration_v1 *c) {
	zwlr_output_configuration_v1_destroy(c); om.pending_config = NULL;
	toast("Screens changed while applying; check and apply again");
}
static const struct zwlr_output_configuration_v1_listener config_listener = { config_succeeded, config_failed, config_cancelled };

static void outputs_apply(void) {
	if (!om.mgr || !om.have_serial || om.pending_config) return;
	if (!outputs_valid()) return;
	om.submitted_n = om.n; memcpy(om.submitted, om.heads, sizeof(om.heads));
	struct zwlr_output_configuration_v1 *c = zwlr_output_manager_v1_create_configuration(om.mgr, om.serial);
	zwlr_output_configuration_v1_add_listener(c, &config_listener, NULL);
	for (int i = 0; i < om.n; i++) {
		struct ohead *hd = &om.heads[i];
		if (!hd->live) continue;
		if (!hd->p_enabled) { zwlr_output_configuration_v1_disable_head(c, hd->h); continue; }
		struct zwlr_output_configuration_head_v1 *ch = zwlr_output_configuration_v1_enable_head(c, hd->h);
		if (hd->p_mode >= 0 && hd->p_mode < hd->nmodes && hd->modes[hd->p_mode].m)
			zwlr_output_configuration_head_v1_set_mode(ch, hd->modes[hd->p_mode].m);
		zwlr_output_configuration_head_v1_set_position(ch, atoi(hd->fx.buf), atoi(hd->fy.buf));
		zwlr_output_configuration_head_v1_set_transform(ch, hd->p_transform);
		zwlr_output_configuration_head_v1_set_scale(ch, wl_fixed_from_double(hd->p_scale));
	}
	zwlr_output_configuration_v1_apply(c);
	om.pending_config = c;
}

// --------------------------------------------------------- wayland window

struct buffer { struct wl_buffer *wb; void *data; int w, h; bool busy; };
static struct {
	struct wl_display *display; struct wl_registry *registry;
	struct wl_compositor *compositor; struct wl_shm *shm; struct xdg_wm_base *wm_base;
	struct wl_seat *seat; struct wl_keyboard *kbd; struct wl_pointer *ptr;
	struct wl_surface *surface; struct xdg_surface *xsurf; struct xdg_toplevel *top;
	struct wl_cursor_theme *cursor_theme; struct wl_surface *cursor_surface;
	int w, h, pending_w, pending_h; bool configured, closed, dirty;
	struct buffer bufs[2];
	struct xkb_context *xkb; struct xkb_keymap *keymap; struct xkb_state *xstate;
	struct xkb_compose_state *compose;
	int repeat_rate, repeat_delay, repeat_fd; uint32_t repeat_key;
	double px, py; uint32_t ptr_serial;
	bool render_only, session_lock_available;
} g = { .w = 1100, .h = 760, .repeat_rate = 25, .repeat_delay = 600, .repeat_fd = -1 };

static void request_redraw(void) { g.dirty = true; }

static void buffer_release(void *d, struct wl_buffer *b) { ((struct buffer *)d)->busy = false; }
static const struct wl_buffer_listener buffer_listener = { buffer_release };
static struct buffer *get_buffer(void) {
	for (int i = 0; i < 2; i++) {
		struct buffer *b = &g.bufs[i];
		if (b->busy) continue;
		if (b->wb && (b->w != g.w || b->h != g.h)) { wl_buffer_destroy(b->wb); munmap(b->data, (size_t)b->w * b->h * 4); b->wb = NULL; }
		if (!b->wb) {
			int stride = g.w * 4; size_t size = (size_t)stride * g.h;
			int fd = memfd_create("cube-settings", MFD_CLOEXEC);
			if (fd < 0 || ftruncate(fd, size) < 0) { if (fd >= 0) close(fd); return NULL; }
			b->data = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
			if (b->data == MAP_FAILED) { close(fd); return NULL; }
			struct wl_shm_pool *pool = wl_shm_create_pool(g.shm, fd, size);
			b->wb = wl_shm_pool_create_buffer(pool, 0, g.w, g.h, stride, WL_SHM_FORMAT_ARGB8888);
			wl_shm_pool_destroy(pool); close(fd);
			wl_buffer_add_listener(b->wb, &buffer_listener, b);
			b->w = g.w; b->h = g.h;
		}
		return b;
	}
	return NULL;
}

// ------------------------------------------------------------------ UI core

enum page { PG_NETWORK, PG_DISPLAY, PG_APPEARANCE, PG_KEYBOARD, PG_SOUND, PG_POWER,
	PG_PRIVACY, PG_FIREWALL, PG_SERVICES, PG_USERS, PG_UPDATES, PG_SYSTEM, PG_POINTER, PG_ADVANCED, PG_COUNT };
static const char *page_names[PG_COUNT] = { "Network", "Display", "Appearance", "Keyboard", "Sound",
	"Power", "Privacy", "Firewall", "Services", "Users", "Updates", "System", "Mouse & touchpad", "Advanced" };
static const char *page_keys[PG_COUNT] = { "network", "display", "appearance", "keyboard", "sound",
	"power", "privacy", "firewall", "services", "users", "updates", "system", "pointer", "advanced" };

enum hit_kind { H_BUTTON, H_FIELD, H_SLIDER, H_SIDEBAR };
struct hit { double x, y, w, h; int id; enum hit_kind kind; };
static struct {
	enum page page; int focus, idc, clicked, submit, active, released, key_step;
	double drag_x; struct field *edit; bool edit_password;
	struct hit hits[MAXHITS]; int nhits, modal_first;
	double scroll[PG_COUNT], content_h, view_top, view_h, sidebar_scroll;
	int privacy_section;
	char toast[256]; double toast_until;
	cairo_t *cr; bool focus_visible;
	double cx, cy, cw; // flow cursor for the page content
	double row_x, row_h; bool in_row;
} ui = { .focus = -1, .clicked = -1, .submit = -1, .active = -1, .released = -1 };

static void toast(const char *fmt, ...) {
	va_list ap; va_start(ap, fmt);
	vsnprintf(ui.toast, sizeof(ui.toast), fmt, ap);
	va_end(ap);
	ui.toast_until = now_s() + 5;
	request_redraw();
}

static void spawn_detached(const char *cmd) {
    char **argv = NULL;
    if (!g_shell_parse_argv(cmd, NULL, &argv, NULL) || !argv || !argv[0]) {
        toast("Cannot launch: invalid command"); return;
    }
    struct job *j = NULL;
    for (int i = 0; i < MAXJOBS; i++) if (!jobs[i].used) { j = &jobs[i]; break; }
    if (!j) { toast("Too many tasks running; try again"); g_strfreev(argv); return; }
    int errors[2];
    if (pipe2(errors, O_CLOEXEC) < 0) { toast("Cannot launch: %s", strerror(errno)); g_strfreev(argv); return; }
    pid_t pid = fork();
    if (pid == 0) {
        close(errors[0]);
        pid_t detached = fork();
        if (detached < 0) { dprintf(errors[1], "%s", strerror(errno)); _exit(127); }
        if (detached > 0) _exit(0);
        setsid();
        sigset_t empty; sigemptyset(&empty); sigprocmask(SIG_SETMASK, &empty, NULL);
        signal(SIGCHLD, SIG_DFL); signal(SIGPIPE, SIG_DFL);
        int null = open("/dev/null", O_RDONLY); if (null >= 0) { dup2(null, 0); if (null > 2) close(null); }
        execvp(argv[0], argv);
        dprintf(errors[1], "%s: %s", argv[0], strerror(errno)); _exit(127);
    }
    close(errors[1]);
    if (pid < 0) { close(errors[0]); toast("Cannot launch: %s", strerror(errno)); g_strfreev(argv); return; }
    fcntl(errors[0], F_SETFL, O_NONBLOCK);
    *j = (struct job){.used = true, .pid = pid, .out = -1, .err = errors[0], .table = -1, .priv = P_LAUNCH, .deadline = now_s() + 10};
    copy(j->label, sizeof(j->label), argv[0]); g_strfreev(argv);
}

// Palette: whitecube's light theme, or high contrast.
static double C_BG[3] = {.984, .984, .984}, C_INK[3] = {.05, .05, .05}, C_MUTED[3] = {.42, .42, .42},
	C_LINE[3] = {.82, .82, .82}, C_SIDE[3] = {.955, .955, .955}, C_GOOD[3] = {.10, .45, .22},
	C_BAD[3] = {.70, .12, .10}, C_WARN[3] = {.62, .42, .02}, C_WHITE3[3] = {1, 1, 1}, C_SURFACE[3] = {1, 1, 1};
static void color_from_hex(double out[3], const char *hex) {
	unsigned v = (unsigned)strtoul(hex, NULL, 16);
	for (int i = 0; i < 3; i++) out[i] = ((v >> (16 - 8 * i)) & 255) / 255.0;
}
static void settings_palette(void) {
	struct status_theme t; status_theme_defaults(&t);
	status_theme_color(t.background, cfg_get("bg", "fbfbfb"));
	status_theme_color(t.bar, cfg_get("tabbar", "f5f5f5"));
	status_theme_finish(&t, acc.strong);
	color_from_hex(C_BG, t.background); color_from_hex(C_SIDE, t.panel);
	color_from_hex(C_INK, t.ink); color_from_hex(C_MUTED, t.muted);
	color_from_hex(C_SURFACE, t.surface); color_from_hex(C_LINE, acc.strong ? t.ink : t.muted);
	// Foreground for controls filled with the theme's ink.
	color_from_hex(C_WHITE3, t.background);
	bool dark = status_theme_luminance(t.background) < .18;
	color_from_hex(C_GOOD, dark ? "7bdca0" : "197338");
	color_from_hex(C_BAD, dark ? "ff9b98" : "b31f1a");
	color_from_hex(C_WARN, dark ? "ebc57b" : "875c02");
}
static void set_rgb(const double c[3]) { cairo_set_source_rgb(ui.cr, c[0], c[1], c[2]); }

static PangoFontDescription *font_for(double px, bool bold, bool mono) {
	static PangoFontDescription *cache[3][2]; static double cached_px[3][2];
	int fam = mono ? 2 : 0;
	if (cache[fam][bold] && cached_px[fam][bold] == px) return cache[fam][bold];
	if (cache[fam][bold]) pango_font_description_free(cache[fam][bold]);
	PangoFontDescription *d = pango_font_description_from_string(mono ? "DejaVu Sans Mono" : "DejaVu Sans");
	pango_font_description_set_absolute_size(d, px * PANGO_SCALE);
	pango_font_description_set_weight(d, bold ? PANGO_WEIGHT_BOLD : PANGO_WEIGHT_NORMAL);
	cache[fam][bold] = d; cached_px[fam][bold] = px;
	return d;
}
// Draw (or just measure, when color is NULL) text; returns height; *w_out gets width.
static double text_ex(double x, double y, const char *s, double px, bool bold, bool mono, const double *color,
		      double maxw, bool wrap, double *w_out) {
	PangoLayout *l = pango_cairo_create_layout(ui.cr);
	pango_layout_set_font_description(l, font_for(px, bold, mono));
	pango_layout_set_text(l, s ? s : "", -1);
	if (maxw > 0) {
		pango_layout_set_width(l, (int)(maxw * PANGO_SCALE));
		if (wrap) pango_layout_set_wrap(l, PANGO_WRAP_WORD_CHAR);
		else pango_layout_set_ellipsize(l, PANGO_ELLIPSIZE_END);
	}
	int w, h; pango_layout_get_pixel_size(l, &w, &h);
	if (color) { set_rgb(color); cairo_move_to(ui.cr, x, y); pango_cairo_show_layout(ui.cr, l); }
	g_object_unref(l);
	if (w_out) *w_out = w;
	return h;
}
static double text(double x, double y, const char *s, double px, bool bold, const double *color, double maxw) {
	return text_ex(x, y, s, px, bold, false, color, maxw, false, NULL);
}
static double text_w(const char *s, double px, bool bold) {
	double w; text_ex(0, 0, s, px, bold, false, NULL, 0, false, &w); return w;
}
static void rect(double x, double y, double w, double h, const double c[3]) {
	set_rgb(c); cairo_rectangle(ui.cr, x, y, w, h); cairo_fill(ui.cr);
}
static void frame_rect(double x, double y, double w, double h, const double c[3], double lw) {
	set_rgb(c); cairo_set_line_width(ui.cr, lw);
	cairo_rectangle(ui.cr, x + lw / 2, y + lw / 2, w - lw, h - lw); cairo_stroke(ui.cr);
}

static int add_hit(double x, double y, double w, double h, enum hit_kind kind) {
	int id = ++ui.idc;
	if (ui.nhits < MAXHITS)
		ui.hits[ui.nhits++] = (struct hit){x, y, w, h, id, kind};
	return id;
}
static bool is_focused(int id) { return ui.focus == id; }
static void focus_ring(double x, double y, double w, double h, int id) {
	if (!is_focused(id) || !ui.focus_visible) return;
	set_rgb(C_INK); cairo_set_line_width(ui.cr, 2);
	double d[] = {3, 3}; cairo_set_dash(ui.cr, d, 2, 0);
	cairo_rectangle(ui.cr, x - 3, y - 3, w + 6, h + 6); cairo_stroke(ui.cr);
	cairo_set_dash(ui.cr, NULL, 0, 0);
}

// ---- flow layout for page content

static double F(void) { return 14 * S; }          // body font
static double RH(void) { return 34 * S; }         // control height
static double GAP(void) { return 10 * S; }

static void flow_newline(void) {
	if (ui.in_row) { ui.cy += ui.row_h + GAP(); ui.in_row = false; }
}
static void place(double w, double h, double *x, double *y) {
	if (!ui.in_row) { ui.in_row = true; ui.row_x = ui.cx; ui.row_h = 0; }
	if (ui.row_x > ui.cx && ui.row_x + w > ui.cx + ui.cw) { ui.cy += ui.row_h + GAP(); ui.row_x = ui.cx; ui.row_h = 0; }
	*x = ui.row_x; *y = ui.cy;
	ui.row_x += w + GAP();
	if (h > ui.row_h) ui.row_h = h;
}
static void heading(const char *s) {
	flow_newline();
	ui.cy += 6 * S;
	ui.cy += text(ui.cx, ui.cy, s, 17 * S, true, C_INK, ui.cw);
	rect(ui.cx, ui.cy + 4 * S, ui.cw, 1.5, C_INK);
	ui.cy += 14 * S;
}
static void para(const char *s, const double *color) {
	flow_newline();
	ui.cy += text_ex(ui.cx, ui.cy, s, 13 * S, false, false, color ? color : C_MUTED, ui.cw, true, NULL) + 8 * S;
}
static void kv(const char *k, const char *v, const double *vcolor) {
	flow_newline();
	double lw = 190 * S;
	text(ui.cx, ui.cy, k, F(), false, C_MUTED, lw - 8);
	double h = text_ex(ui.cx + lw, ui.cy, v && *v ? v : "—", F(), false, false, vcolor ? vcolor : C_INK, ui.cw - lw, true, NULL);
	ui.cy += (h > 20 * S ? h : 20 * S) + 6 * S;
}
static void spacer(double h) { flow_newline(); ui.cy += h * S; }

// A cube: the checkbox/radio/bullet shape everywhere in whitecube.
static void cube(double x, double y, double s, bool filled) {
	frame_rect(x, y, s, s, C_INK, 1.5);
	if (filled) rect(x + 3.5, y + 3.5, s - 7, s - 7, C_INK);
}

static bool button_ex(const char *label, bool primary, bool danger, bool enabled) {
	double tw = text_w(label, F(), primary);
	double w = fmin(ui.cw, tw + 28 * S), h = RH(), x, y;
	place(w, h, &x, &y);
	int id = add_hit(x, y, w, h, H_BUTTON);
	const double *fg = !enabled ? C_LINE : danger ? C_BAD : C_INK;
	if (primary && enabled) rect(x, y, w, h, danger ? C_BAD : C_INK);
	else rect(x, y, w, h, C_SURFACE);
	frame_rect(x, y, w, h, fg, 1.5);
	text(x + 14 * S, y + (h - F() * 1.25) / 2, label, F(), primary, primary && enabled ? C_WHITE3 : fg, w - 28 * S);
	focus_ring(x, y, w, h, id);
	bool hit = enabled && ui.clicked == id;
	if (hit) ui.clicked = -1;
	return hit;
}
static bool button(const char *label) { return button_ex(label, false, false, true); }

static bool toggle(const char *label, bool on, bool enabled) {
	double s = 18 * S, tw = text_w(label, F(), false);
	double w = fmin(ui.cw, s + 10 * S + tw + 6 * S), h = RH(), x, y;
	place(w, h, &x, &y);
	int id = add_hit(x, y, w, h, H_BUTTON);
	cube(x, y + (h - s) / 2, s, on);
	text(x + s + 10 * S, y + (h - F() * 1.25) / 2, label, F(), false, enabled ? C_INK : C_MUTED, w - s - 16 * S);
	focus_ring(x, y, w, h, id);
	bool hit = enabled && ui.clicked == id;
	if (hit) ui.clicked = -1;
	return hit;
}
// Integer slider: live value in *v while dragging; returns true when committed.
static bool slider(const char *label, int *v, int lo, int hi, const char *unit, bool enabled) {
	flow_newline();
	double lw = 190 * S, x = ui.cx + lw, y = ui.cy, w = fmin(ui.cw - lw - 80 * S, 420 * S), h = RH();
	text(ui.cx, y + (h - F() * 1.25) / 2, label, F(), false, C_MUTED, lw - 8);
	int id = add_hit(x, y, w, h, H_SLIDER);
	bool commit = false;
	if (enabled && ui.active == id) {
		double t = (ui.drag_x - x) / w; t = t < 0 ? 0 : t > 1 ? 1 : t;
		*v = lo + (int)lround(t * (hi - lo));
	}
	if (enabled && ui.released == id) { commit = true; ui.released = -1; }
	if (enabled && is_focused(id) && ui.key_step) {
		*v += ui.key_step * ((hi - lo) >= 50 ? 5 : 1);
		ui.key_step = 0; commit = true;
	}
	if (*v < lo) *v = lo;
	if (*v > hi) *v = hi;
	double t = hi > lo ? (double)(*v - lo) / (hi - lo) : 0;
	rect(x, y + h / 2 - 1, w, 2, enabled ? C_INK : C_LINE);
	double k = 16 * S, kx = x + t * w - k / 2;
	rect(kx, y + (h - k) / 2, k, k, enabled ? C_INK : C_LINE);
	char val[48]; snprintf(val, sizeof(val), "%d%s", *v, unit);
	text(x + w + 14 * S, y + (h - F() * 1.25) / 2, val, F(), true, enabled ? C_INK : C_MUTED, 0);
	focus_ring(x, y, w, h, id);
	ui.cy += h + 6 * S;
	return commit;
}
// "◀ value ▶" stepper; returns -1, 0 or +1.
static int stepper(const char *label, const char *value) {
	flow_newline();
	double lw = 190 * S, h = RH(), b = h, x = ui.cx + lw, y = ui.cy;
	double vw = fmax(text_w(value, F(), true) + 24 * S, 150 * S);
	text(ui.cx, y + (h - F() * 1.25) / 2, label, F(), false, C_MUTED, lw - 8);
	int r = 0;
	int idl = add_hit(x, y, b, h, H_BUTTON);
	frame_rect(x, y, b, h, C_INK, 1.5);
	text(x + b / 2 - 5 * S, y + (h - F() * 1.25) / 2, "◀", F(), false, C_INK, 0);
	focus_ring(x, y, b, h, idl);
	frame_rect(x + b - 1.5, y, vw, h, C_INK, 1.5);
	double tw = text_w(value, F(), true);
	text(x + b + (vw - tw) / 2, y + (h - F() * 1.25) / 2, value, F(), true, C_INK, 0);
	int idr = add_hit(x + b + vw - 3, y, b, h, H_BUTTON);
	frame_rect(x + b + vw - 3, y, b, h, C_INK, 1.5);
	text(x + b + vw - 3 + b / 2 - 5 * S, y + (h - F() * 1.25) / 2, "▶", F(), false, C_INK, 0);
	focus_ring(x + b + vw - 3, y, b, h, idr);
	if (ui.clicked == idl) { r = -1; ui.clicked = -1; }
	if (ui.clicked == idr) { r = 1; ui.clicked = -1; }
	ui.cy += h + 6 * S;
	return r;
}
// Single-line text field; returns true on Enter.
static bool textfield(struct field *f, const char *placeholder, double w, bool password) {
	double h = RH(), x, y;
	place(w, h, &x, &y);
	int id = add_hit(x, y, w, h, H_FIELD);
	rect(x, y, w, h, C_SURFACE);
	frame_rect(x, y, w, h, is_focused(id) ? C_INK : C_MUTED, is_focused(id) ? 2 : 1.2);
	char shown[256];
	if (password) { size_t n = strlen(f->buf); n = n > 64 ? 64 : n; for (size_t i = 0; i < n; i++) shown[i] = '*'; shown[n] = 0; }
	else copy(shown, sizeof(shown), f->buf);
	double ty = y + (h - F() * 1.25) / 2;
	if (!shown[0] && !is_focused(id)) text(x + 10 * S, ty, placeholder, F(), false, C_MUTED, w - 20 * S);
	else {
		double tw; text_ex(x + 10 * S, ty, shown, F(), false, false, C_INK, w - 20 * S, false, &tw);
		if (is_focused(id)) rect(x + 10 * S + fmin(tw, w - 22 * S) + 1, ty, 1.5, F() * 1.25, C_INK);
	}
	if (is_focused(id)) { ui.edit = f; ui.edit_password = password; }
	bool submit = ui.submit == id;
	if (submit) ui.submit = -1;
	return submit;
}
// Selectable list row with optional right-hand detail; returns true when clicked.
static bool list_row(const char *title, const char *detail, bool selected, const double *detail_color) {
	flow_newline();
	double h = 30 * S, x = ui.cx, y = ui.cy, w = ui.cw;
	int id = add_hit(x, y, w, h, H_BUTTON);
	if (selected) rect(x, y, w, h, C_INK);
	else rect(x, y + h - 1, w, 1, C_LINE);
	cube(x + 6 * S, y + (h - 12 * S) / 2, 12 * S, selected);
	if (selected) frame_rect(x + 6 * S, y + (h - 12 * S) / 2, 12 * S, 12 * S, C_WHITE3, 1.5);
	double dw = detail && *detail ? fmin(text_w(detail, 13 * S, false), w * .45) : 0;
	text(x + 28 * S, y + (h - F() * 1.25) / 2, title, F(), selected, selected ? C_WHITE3 : C_INK, w - 40 * S - dw);
	if (dw > 0) text(x + w - dw - 10 * S, y + (h - 13 * S * 1.25) / 2, detail, 13 * S, false,
			 selected ? C_WHITE3 : detail_color ? detail_color : C_MUTED, dw);
	focus_ring(x, y, w, h, id);
	ui.cy += h;
	bool hit = ui.clicked == id;
	if (hit) ui.clicked = -1;
	return hit;
}
static void row_end(void) { flow_newline(); }
static void status_line(int t) {
	if (tables[t].loading && !tables[t].loaded) para("Loading…", NULL);
	else if (tables[t].err[0]) para(tables[t].err, C_BAD);
}

// ------------------------------------------------------------- page state

static struct {
	// network
	bool scanned;
	// appearance
	struct field col[5], col_loaded[5]; bool col_init; struct field app_term, app_launcher, app_browser, apps_loaded[3];
	// keyboard
	struct field kb_filter, kb_test; char kb_layout[64], kb_variant[64]; bool kb_init; int rate, delay;
	char variants_for[64];
	char kb_loaded[2][64]; int rate_loaded, delay_loaded;
	// sound / power
	int vol, bright; bool vol_drag, bright_drag;
	// system
	struct field host, tz_filter; bool host_init;
	// users
	char selected_user[64];
} st = { .vol = -1, .bright = -1 };

static const char *COLOR_KEYS[5] = {"bg", "grid", "tabbar", "border", "unfocused"};
static const char *COLOR_NAMES[5] = {"Desktop background", "Grid lines", "Tab bar", "Focused border", "Unfocused tabs"};
static const struct { const char *name; const char *c[5]; } THEMES[] = {
	{"White cube", {"fbfbfb", "e0e0e0", "f5f5f5", "0d0d0d", "d4d4d4"}},
	{"Space", {"06080d", "0c0f15", "090c12", "697c99", "8995a8"}},
	{"Graphite", {"1b1b1b", "262626", "202020", "e6e6e6", "5a5a5a"}},
	{"Paper", {"f4efe6", "e3dccf", "ece6da", "2b2620", "cfc6b6"}},
};

static void refresh_page(bool force) {
	double age_limit = force ? 0 : 3;
	#define STALE(t) (force || !tables[t].loaded || now_s() - tables[t].stamp > age_limit)
	switch (ui.page) {
	case PG_PRIVACY: if (STALE(T_PRIVACY)) load(T_PRIVACY, P_PRIVACY, 1, "status"); break;
	case PG_NETWORK: if (STALE(T_NET)) load(T_NET, P_USER, 1, "net"); break;
	case PG_SOUND: if (STALE(T_SOUND) && !st.vol_drag) load(T_SOUND, P_USER, 1, "sound"); break;
	case PG_POWER: if (STALE(T_POWER) && !st.bright_drag) load(T_POWER, P_USER, 1, "power"); break;
	case PG_FIREWALL: if (force || !tables[T_FW].loaded) load(T_FW, P_FREE, 1, "firewall"); break;
	case PG_SERVICES: if (force || !tables[T_SERVICES].loaded) load(T_SERVICES, P_FREE, 1, "services"); break;
	case PG_USERS: if (force || !tables[T_USERS].loaded) load(T_USERS, P_USER, 1, "users"); break;
	case PG_UPDATES: if (force || !tables[T_UPDATES].loaded) load(T_UPDATES, P_USER, 1, "updates"); break;
	case PG_KEYBOARD: if (!tables[T_LAYOUTS].loaded) load(T_LAYOUTS, P_USER, 1, "layouts"); break;
	case PG_SYSTEM:
		if (STALE(T_SYS)) load(T_SYS, P_USER, 1, "system");
		if (force || !tables[T_DISKS].loaded) load(T_DISKS, P_USER, 1, "disks");
		if (!tables[T_ZONES].loaded) load(T_ZONES, P_USER, 1, "timezones");
		break;
	default: break;
	}
	#undef STALE
}

// ------------------------------------------------------------------ pages

static void page_network(void) {
	heading("Wi-Fi");
	status_line(T_NET);
	const char *present = tv(T_NET, "net", "present"), *state = tv(T_NET, "net", "association_state");
	bool have = yes(present);
	const char *svc = tv(T_NET, "net", "service");
	kv("Adapter", tables[T_NET].loaded ? (have ? tv(T_NET, "net", "interface") : "D-Link dongle not plugged in") : "", have ? NULL : C_BAD);
	kv("Connection", state, !strcmp(state, "connected") || !strcmp(state, "completed") ? C_GOOD : NULL);
	const char *sig = tv(T_NET, "net", "signal_dbm");
	char sigtxt[64] = "";
	if (*sig) { int d = atoi(sig); snprintf(sigtxt, sizeof(sigtxt), "%s dBm (%s)", sig, d > -55 ? "excellent" : d > -67 ? "good" : d > -75 ? "fair" : "weak"); }
	kv("Signal", sigtxt, NULL);
	kv("IPv4 address", tv(T_NET, "net", "ipv4_address"), NULL);
	kv("Gateway", tv(T_NET, "net", "default_route"), NULL);
	kv("DNS servers", tv(T_NET, "net", "dns_servers"), NULL);
	const char *inet = tv(T_NET, "net", "connectivity_ok");
	kv("Internet", !*inet ? "" : yes(inet) ? "reachable" : "not reachable", !*inet ? NULL : yes(inet) ? C_GOOD : C_BAD);
	kv("Wi-Fi service", svc, !strcmp(svc, "enabled") ? NULL : C_WARN);
	const char *summary = tv(T_NET, "net", "summary");
	if (*summary) para(summary, NULL);
	if (button("Scan for networks")) { st.scanned = true; load(T_SCAN, P_FREE, 1, "net-scan"); }
	if (button("Add network…")) {
		modal.kind = M_ADD_WIFI; modal.a.buf[0] = modal.b.buf[0] = 0; ui.focus = -1;
		copy(modal.title, sizeof(modal.title), "Add a Wi-Fi network");
		copy(modal.text, sizeof(modal.text), "WPA2 personal. The passphrase is hashed and stored root-only in /etc/whitecube/network.");
		copy(modal.ok_label, sizeof(modal.ok_label), "Save and connect");
	}
	if (button("Restart Wi-Fi")) action("Restart Wi-Fi", P_FREE, 1, "net-reconnect");
	if (strcmp(svc, "enabled") && tables[T_NET].loaded && button_ex("Turn Wi-Fi service on", true, false, true))
		action("Enable Wi-Fi service", P_PASS, 3, "service", "enable", "whitecube-network.service");
	row_end();

	heading("Saved networks");
	int saved = 0;
	for (int i = 0; i < tables[T_NET].n; i++) {
		struct row *r = &tables[T_NET].rows[i];
		if (strcmp(r->f[0], "profile")) continue;
		saved++;
		flow_newline();
		text(ui.cx, ui.cy + (RH() - F() * 1.25) / 2, r->f[2], F(), true, C_INK, ui.cw * .5);
		ui.in_row = true; ui.row_x = ui.cx + ui.cw * .5; ui.row_h = RH();
		if (button("Connect")) action("Connect", P_FREE, 2, "net-connect", r->f[1]);
		if (button("Forget")) {
			char t[160]; snprintf(t, sizeof(t), "Forget %s? Its passphrase is removed from this computer.", r->f[2]);
			struct pending p = make_pending("Forget network", P_PASS, 2, "net-forget", r->f[1]);
			confirm("Forget network", t, "Forget", true, &p);
		}
		row_end();
	}
	if (!saved && tables[T_NET].loaded) para("No saved networks. Scan, or use Add network.", NULL);

	if (st.scanned) {
		heading("Networks in range");
		status_line(T_SCAN);
		if (tables[T_SCAN].loading) para("Scanning…", NULL);
		for (int i = 0; i < tables[T_SCAN].n; i++) {
			struct row *r = &tables[T_SCAN].rows[i];
			if (strcmp(r->f[0], "ap")) continue;
			int dbm = atoi(r->f[2]);
			char detail[96]; snprintf(detail, sizeof(detail), "%s  ·  %s  ·  %s GHz", r->f[4],
				dbm > -55 ? "▮▮▮▮" : dbm > -67 ? "▮▮▮▯" : dbm > -75 ? "▮▮▯▯" : "▮▯▯▯", atoi(r->f[3]) > 4000 ? "5" : "2.4");
			if (list_row(r->f[1], detail, false, NULL)) {
				const char *id = NULL;
				for (int k = 0; k < tables[T_NET].n; k++)
					if (!strcmp(tables[T_NET].rows[k].f[0], "profile") && !strcmp(tables[T_NET].rows[k].f[2], r->f[1])) id = tables[T_NET].rows[k].f[1];
				if (id) action("Connect", P_FREE, 2, "net-connect", id);
				else if (!strcmp(r->f[4], "open")) toast("Open networks are not supported by cube-net (WPA2 only)");
				else {
					modal.kind = M_ADD_WIFI; copy(modal.a.buf, sizeof(modal.a.buf), r->f[1]); modal.b.buf[0] = 0; ui.focus = -1;
					copy(modal.title, sizeof(modal.title), "Connect to Wi-Fi");
					copy(modal.text, sizeof(modal.text), "Enter the WPA2 passphrase. It is hashed and stored root-only.");
					copy(modal.ok_label, sizeof(modal.ok_label), "Save and connect");
				}
			}
		}
	}
}

static const char *transform_name(int t) {
	static const char *n[] = {"Normal", "90°", "180°", "270°", "Flipped", "Flipped 90°", "Flipped 180°", "Flipped 270°"};
	return t >= 0 && t < 8 ? n[t] : "?";
}
static void page_display(void) {
	if (!om.mgr) {
		heading("Displays");
		para(g.render_only ? "Display settings are read live from the compositor; open the panel inside whitecube."
			: "This compositor does not offer output management. Log out and back in after updating whitecube.", C_WARN);
		return;
	}
	for (int i = 0; i < om.n; i++) {
		struct ohead *hd = &om.heads[i];
		if (!hd->live) continue;
		char title[256];
		snprintf(title, sizeof(title), "%s  %s %s", hd->name, hd->make, hd->model);
		heading(title);
		if (hd->pw > 0) { char size[64]; snprintf(size, sizeof(size), "%d × %d mm (%.1f\")", hd->pw, hd->ph, sqrt(hd->pw * hd->pw + hd->ph * hd->ph) / 25.4); kv("Physical size", size, NULL); }
		if (toggle("Screen on", hd->p_enabled, true)) { hd->p_enabled = !hd->p_enabled; hd->edited = true; }
		row_end();
		if (!hd->p_enabled) continue;
		char mode[64] = "auto";
		if (hd->p_mode >= 0 && hd->p_mode < hd->nmodes) {
			struct omode *m = &hd->modes[hd->p_mode];
			snprintf(mode, sizeof(mode), "%d × %d @ %.2f Hz%s", m->w, m->h, m->refresh / 1000.0, m->preferred ? " ★" : "");
		}
		int d = stepper("Resolution", mode);
		if (d && hd->nmodes) { hd->p_mode = (hd->p_mode + d + hd->nmodes) % hd->nmodes; hd->edited = true; }
		char scale[16]; snprintf(scale, sizeof(scale), "%.2f×", hd->p_scale);
		d = stepper("Scale", scale);
		if (d) { hd->p_scale = fmin(3, fmax(.5, round((hd->p_scale + d * .25) * 4) / 4)); hd->edited = true; }
		d = stepper("Rotation", transform_name(hd->p_transform));
		if (d) { hd->p_transform = (hd->p_transform + d + 8) % 8; hd->edited = true; }
		flow_newline();
		text(ui.cx, ui.cy + 8 * S, "Position (x, y)", F(), false, C_MUTED, 0);
		ui.in_row = true; ui.row_x = ui.cx + 190 * S; ui.row_h = 0;
		struct field before_x = hd->fx, before_y = hd->fy;
		textfield(&hd->fx, "0", 100 * S, false);
		textfield(&hd->fy, "0", 100 * S, false);
		if (strcmp(before_x.buf, hd->fx.buf) || strcmp(before_y.buf, hd->fy.buf)) hd->edited = true;
		row_end();
	}
	if (!om.n) para("No screens reported yet.", NULL);
	spacer(6);
	bool edited = false;
	for (int i = 0; i < om.n; i++) edited |= om.heads[i].edited;
	if (button_ex(om.pending_config ? "Applying…" : "Apply", true, false, edited && !om.pending_config)) outputs_apply();
	if (button_ex("Revert", false, false, edited && !om.pending_config)) for (int i = 0; i < om.n; i++) head_reset_pending(&om.heads[i]);
	row_end();
	para("Applied settings are saved to ~/.config/whitecube/outputs and restored at login. Scale also resizes whitecube's tabs and grid.", NULL);
}

static bool valid_hex(const char *s) {
	if (*s == '#') s++;
	if (strlen(s) != 6) return false;
	for (int i = 0; i < 6; i++) if (!isxdigit((unsigned char)s[i])) return false;
	return true;
}
static void page_appearance(void) {
	if (!st.col_init) {
		for (int i = 0; i < 5; i++) copy(st.col[i].buf, sizeof(st.col[i].buf), cfg_get(COLOR_KEYS[i], ""));
		copy(st.app_term.buf, 256, cfg_get("terminal", "")); copy(st.app_launcher.buf, 256, cfg_get("launcher", ""));
		copy(st.app_browser.buf, 256, cfg_get("browser_cmd", ""));
		for (int i = 0; i < 5; i++) st.col_loaded[i] = st.col[i];
		st.apps_loaded[0] = st.app_term; st.apps_loaded[1] = st.app_launcher; st.apps_loaded[2] = st.app_browser;
		st.col_init = true;
	}
	for (int i = 0; i < 5; i++) {
		const char *current = cfg_get(COLOR_KEYS[i], THEMES[0].c[i]);
		if (!strcmp(st.col[i].buf, st.col_loaded[i].buf)) copy(st.col[i].buf, 256, current);
		copy(st.col_loaded[i].buf, 256, current);
	}
	struct field *app_fields[] = {&st.app_term, &st.app_launcher, &st.app_browser};
	const char *app_keys[] = {"terminal", "launcher", "browser_cmd"};
	for (int i = 0; i < 3; i++) {
		const char *current = cfg_get(app_keys[i], "");
		if (!strcmp(app_fields[i]->buf, st.apps_loaded[i].buf)) copy(app_fields[i]->buf, 256, current);
		copy(st.apps_loaded[i].buf, 256, current);
	}
	heading("Theme");
	para("Applies to whitecube within a second. Presets set all five colours.", NULL);
	for (size_t t = 0; t < sizeof(THEMES) / sizeof(THEMES[0]); t++) {
		bool current = true;
		for (int i = 0; i < 5; i++) current &= !strcasecmp(cfg_get(COLOR_KEYS[i], ""), THEMES[t].c[i]);
		if (button_ex(THEMES[t].name, current, false, true)) {
			bool queued = cfg_set_many(5, COLOR_KEYS, THEMES[t].c);
			for (int i = 0; i < 5; i++) copy(st.col[i].buf, 256, THEMES[t].c[i]);
			if (queued) toast("Saving theme %s…", THEMES[t].name);
		}
	}
	row_end();
	heading("Colours");
	bool submit = false;
	for (int i = 0; i < 5; i++) {
		flow_newline();
		double y = ui.cy, sw = RH();
		unsigned r = 128, gg = 128, b = 128;
		const char *hx = st.col[i].buf[0] == '#' ? st.col[i].buf + 1 : st.col[i].buf;
		bool ok = valid_hex(st.col[i].buf) && sscanf(hx, "%02x%02x%02x", &r, &gg, &b) == 3;
		cairo_set_source_rgb(ui.cr, r / 255.0, gg / 255.0, b / 255.0);
		cairo_rectangle(ui.cr, ui.cx, y, sw, sw); cairo_fill(ui.cr);
		frame_rect(ui.cx, y, sw, sw, C_INK, 1.5);
		text(ui.cx + sw + 12 * S, y + (RH() - F() * 1.25) / 2, COLOR_NAMES[i], F(), false, C_INK, 0);
		ui.in_row = true; ui.row_x = ui.cx + 230 * S; ui.row_h = RH();
		submit |= textfield(&st.col[i], "rrggbb", 140 * S, false);
		if (!ok) text(ui.row_x, y + (RH() - F() * 1.25) / 2, "6 hex digits", 13 * S, false, C_BAD, 0);
		row_end();
	}
	if (button_ex("Apply colours", true, false, true) || submit) {
		bool all = true;
		for (int i = 0; i < 5; i++) all &= valid_hex(st.col[i].buf);
		if (!all) toast("Each colour needs 6 hex digits, like fbfbfb");
		else {
			const char *vals[5]; char clean[5][8];
			for (int i = 0; i < 5; i++) { copy(clean[i], 8, st.col[i].buf[0] == '#' ? st.col[i].buf + 1 : st.col[i].buf); vals[i] = clean[i]; }
			if (cfg_set_many(5, COLOR_KEYS, vals)) toast("Saving colours…");
		}
	}
	row_end();

	heading("Tabs and grid");
	char v[32];
	int tw = atoi(cfg_get("tab_w", "180")), th = atoi(cfg_get("tab_h", "40")), gs = atoi(cfg_get("grid_step", "64"));
	snprintf(v, sizeof(v), "%d px", tw);
	int d = stepper("Tab width", v);
	if (d) { snprintf(v, sizeof(v), "%d", tw + d * 10 < 60 ? 60 : tw + d * 10 > 400 ? 400 : tw + d * 10); cfg_set("tab_w", v); }
	snprintf(v, sizeof(v), "%d px", th);
	d = stepper("Tab bar height", v);
	if (d) { snprintf(v, sizeof(v), "%d", th + d * 2 < 20 ? 20 : th + d * 2 > 80 ? 80 : th + d * 2); cfg_set("tab_h", v); }
	snprintf(v, sizeof(v), "%d px", gs);
	d = stepper("Grid spacing", v);
	if (d) { snprintf(v, sizeof(v), "%d", gs + d * 8 < 16 ? 16 : gs + d * 8 > 256 ? 256 : gs + d * 8); cfg_set("grid_step", v); }

	heading("Accessibility");
	if (slider("Interface scale", &acc.ui_scale, 80, 160, " %", true)) { acc_save(); S = acc.ui_scale / 100.0; }
	if (slider("Terminal text scale", &acc.terminal_scale, 80, 200, " %", true)) acc_save();
	if (toggle("Strong contrast", acc.strong, true)) { acc.strong = !acc.strong; acc_save(); }
	if (toggle("Reduce motion", acc.reduced_motion, true)) { acc.reduced_motion = !acc.reduced_motion; acc_save(); }
	row_end();

	heading("Clock");
	bool twelve = !strcmp(cfg_get("clock_format", "24h"), "12h");
	if (button_ex("24-hour", !twelve, false, !any_action_running())) cfg_set("clock_format", "24h");
	if (button_ex("12-hour", twelve, false, !any_action_running())) cfg_set("clock_format", "12h");
	row_end();
	bool seconds = !strcmp(cfg_boolean("clock_seconds", "0"), "1");
	if (toggle("Show seconds", seconds, !any_action_running())) cfg_set("clock_seconds", seconds ? "0" : "1");
	row_end();

	heading("Wallpaper");
	para("The wallpaper is the living cube field (cube-ca).", NULL);
	if (button("Next wallpaper")) spawn_detached("whitecube-next-wallpaper");
	if (button("Field controls…")) spawn_detached("/usr/bin/whitecube-terminal -T 'Cube field controls' cube-ca --menu");
	row_end();

	heading("Applications");
	struct { const char *label, *key; struct field *f; } apps[] = {
		{"Terminal (Alt+Enter)", "terminal", &st.app_term}, {"Launcher (Alt+D)", "launcher", &st.app_launcher},
		{"Browser (Alt+W)", "browser_cmd", &st.app_browser},
	};
	for (int i = 0; i < 3; i++) {
		flow_newline();
		text(ui.cx, ui.cy + (RH() - F() * 1.25) / 2, apps[i].label, F(), false, C_MUTED, 0);
		ui.in_row = true; ui.row_x = ui.cx + 190 * S; ui.row_h = RH();
		if (textfield(apps[i].f, "command", fmin(ui.cw - 190 * S, 560 * S), false) && apps[i].f->buf[0]) {
			if (cfg_set(apps[i].key, apps[i].f->buf)) toast("Saving %s…", apps[i].label);
		}
		row_end();
	}
	para("Press Enter in a field to save it.", NULL);
}

static const struct { const char *option, *label; } KB_OPTIONS[] = {
	{"caps:escape", "Caps Lock is Escape"}, {"ctrl:nocaps", "Caps Lock is Ctrl"},
	{"compose:ralt", "Right Alt is Compose"}, {"grp:alt_shift_toggle", "Alt+Shift switches layout"},
	{"altwin:swap_alt_win", "Swap Alt and Super"},
};
static bool option_on(const char *opts, const char *o) {
	size_t n = strlen(o);
	for (const char *p = opts; (p = strstr(p, o)); p += n)
		if ((p == opts || p[-1] == ',') && (p[n] == 0 || p[n] == ',')) return true;
	return false;
}
static void option_toggle(char *opts, size_t cap, const char *o) {
	char out[256] = ""; bool had = option_on(opts, o);
	char tmp[256]; copy(tmp, sizeof(tmp), opts);
	for (char *save = NULL, *t = strtok_r(tmp, ",", &save); t; t = strtok_r(NULL, ",", &save)) {
		if (!strcmp(t, o) || (!had && ((!strcmp(o, "caps:escape") && !strcmp(t, "ctrl:nocaps")) || (!strcmp(o, "ctrl:nocaps") && !strcmp(t, "caps:escape"))))) continue;
		if (out[0]) strncat(out, ",", sizeof(out) - strlen(out) - 1);
		strncat(out, t, sizeof(out) - strlen(out) - 1);
	}
	if (!had) { if (out[0]) strncat(out, ",", sizeof(out) - strlen(out) - 1); strncat(out, o, sizeof(out) - strlen(out) - 1); }
	copy(opts, cap, out);
}
static void page_keyboard(void) {
	if (!st.kb_init) {
		copy(st.kb_layout, sizeof(st.kb_layout), cfg_get("xkb_layout", ""));
		copy(st.kb_variant, sizeof(st.kb_variant), cfg_get("xkb_variant", ""));
		st.rate = atoi(cfg_get("repeat_rate", "25")); st.delay = atoi(cfg_get("repeat_delay", "600"));
		copy(st.kb_loaded[0], 64, st.kb_layout); copy(st.kb_loaded[1], 64, st.kb_variant);
		st.rate_loaded = st.rate; st.delay_loaded = st.delay;
		st.kb_init = true;
	}
	if (!strcmp(st.kb_layout, st.kb_loaded[0]) && !strcmp(st.kb_variant, st.kb_loaded[1])) {
		copy(st.kb_layout, sizeof(st.kb_layout), cfg_get("xkb_layout", ""));
		copy(st.kb_variant, sizeof(st.kb_variant), cfg_get("xkb_variant", ""));
	}
	if (st.rate == st.rate_loaded) st.rate = atoi(cfg_get("repeat_rate", "25"));
	if (st.delay == st.delay_loaded) st.delay = atoi(cfg_get("repeat_delay", "600"));
	copy(st.kb_loaded[0], 64, cfg_get("xkb_layout", "")); copy(st.kb_loaded[1], 64, cfg_get("xkb_variant", ""));
	st.rate_loaded = atoi(cfg_get("repeat_rate", "25")); st.delay_loaded = atoi(cfg_get("repeat_delay", "600"));
	const char *layout = cfg_get("xkb_layout", ""), *variant = cfg_get("xkb_variant", "");
	char opts[256]; copy(opts, sizeof(opts), cfg_get("xkb_options", ""));
	heading("Layout");
	char cur[160]; snprintf(cur, sizeof(cur), "%s%s%s", *layout ? layout : "us (default)", *variant ? " / " : "", variant);
	kv("In use", cur, NULL);
	status_line(T_LAYOUTS);
	flow_newline();
	textfield(&st.kb_filter, "Filter layouts, e.g. german or fr", fmin(ui.cw, 420 * S), false);
	row_end();
	int shown = 0;
	for (int i = 0; i < tables[T_LAYOUTS].n && shown < 8; i++) {
		struct row *r = &tables[T_LAYOUTS].rows[i];
		if (st.kb_filter.buf[0] ? !strcasestr(r->f[2], st.kb_filter.buf) && strcasecmp(r->f[1], st.kb_filter.buf)
		                        : strcmp(r->f[1], st.kb_layout) != 0) continue;
		shown++;
		if (list_row(r->f[2], r->f[1], !strcmp(r->f[1], st.kb_layout), NULL)) {
			copy(st.kb_layout, sizeof(st.kb_layout), r->f[1]); st.kb_variant[0] = 0;
		}
	}
	if (!st.kb_filter.buf[0]) para("Type in the filter to choose another layout.", NULL);
	if (st.kb_layout[0] && !strchr(st.kb_layout, ',') && strcmp(st.variants_for, st.kb_layout)) {
		copy(st.variants_for, sizeof(st.variants_for), st.kb_layout);
		table_set(&tables[T_VARIANTS], "");
		load(T_VARIANTS, P_USER, 2, "variants", st.kb_layout);
	}
	if (st.kb_layout[0] && !strchr(st.kb_layout, ',')) {
		heading("Variant");
		if (list_row("Default", "", !st.kb_variant[0], NULL)) st.kb_variant[0] = 0;
		int nv = 0;
		for (int i = 0; i < tables[T_VARIANTS].n && nv < 40; i++, nv++) {
			struct row *r = &tables[T_VARIANTS].rows[i];
			if (list_row(r->f[2], r->f[1], !strcmp(r->f[1], st.kb_variant), NULL))
				copy(st.kb_variant, sizeof(st.kb_variant), r->f[1]);
		}
	}
	heading("Options");
	for (size_t i = 0; i < sizeof(KB_OPTIONS) / sizeof(KB_OPTIONS[0]); i++) {
		if (toggle(KB_OPTIONS[i].label, option_on(opts, KB_OPTIONS[i].option), true)) {
			option_toggle(opts, sizeof(opts), KB_OPTIONS[i].option);
			cfg_set("xkb_options", opts);
		}
		row_end();
	}
	heading("Key repeat");
	slider("Repeat rate (0 = off)", &st.rate, 0, 100, " /s", true);
	slider("Delay before repeat", &st.delay, 100, 2000, " ms", true);
	spacer(4);
	bool changed = strcmp(st.kb_layout, layout) || strcmp(st.kb_variant, variant) ||
		st.rate != atoi(cfg_get("repeat_rate", "25")) || st.delay != atoi(cfg_get("repeat_delay", "600"));
	if (button_ex("Apply to desktop", true, false, changed)) {
		char rate[16], delay[16];
		snprintf(rate, sizeof(rate), "%d", st.rate); snprintf(delay, sizeof(delay), "%d", st.delay);
		const char *k[4] = {"xkb_layout", "xkb_variant", "repeat_rate", "repeat_delay"};
		const char *v[4] = {st.kb_layout, st.kb_variant, rate, delay};
		if (cfg_set_many(4, k, v)) toast("Saving keyboard preferences…");
	}
	if (button_ex("Also use for text consoles…", false, false, st.kb_layout[0] != 0))
		action("Console keyboard", P_PASS, 3, "console-keyboard", st.kb_layout, st.kb_variant);
	row_end();
	heading("Try it");
	flow_newline();
	textfield(&st.kb_test, "Type here to test the layout", fmin(ui.cw, 560 * S), false);
	row_end();
}

/* These controls share the same dconf keys as the Advanced editor. */
static void native_toggle(const char *key, const char *label, const char *fallback) {
	bool on = !strcmp(cfg_boolean(key, fallback), "1");
	if (toggle(label, on, !any_action_running())) cfg_set(key, on ? "0" : "1");
	row_end();
}
static void page_pointer(void) {
	heading("Mouse & touchpad");
	para("Changes apply to supported devices within one second in the updated compositor, including devices connected later. Devices without a capability keep their own behaviour.", NULL);
	static int speed; static bool dragging;
	if (!dragging) speed = atoi(cfg_get("pointer_speed", "0"));
	int before = speed;
	bool commit = slider("Pointer speed", &speed, -100, 100, " %", !any_action_running());
	dragging = ui.active >= 0 && (dragging || speed != before);
	if (commit) { char value[16]; snprintf(value, sizeof(value), "%d", speed); cfg_set("pointer_speed", value); dragging = false; }
	native_toggle("natural_scroll", "Natural scrolling", "0");
	native_toggle("left_handed", "Left-handed buttons", "0");
	heading("Touchpad");
	native_toggle("tap_to_click", "Tap to click", "0");
	native_toggle("disable_while_typing", "Disable while typing", "1");
	para("Advanced shows which values you have overridden. Resetting an input preference restores the device default unless your config file specifies a value.", NULL);
}

static struct {
	struct field search, value;
	char selected[64], last_value[256], last_search[256];
	bool modified_only;
	int offset;
} advanced;
static int pref_compare(const void *a, const void *b) { return strcmp(*(char *const *)a, *(char *const *)b); }
static const char *pref_origin(const char *key) {
	for (int i = 0; i < ncfg_prefs; i++) if (!strcmp(cfg_prefs[i].key, key)) return "Your dconf override";
	for (int i = 0; i < ncfg_user; i++) if (!strcmp(cfg_user[i].key, key)) return "Your Whitecube config file";
	for (int i = 0; i < ncfg_sys; i++) if (!strcmp(cfg_sys[i].key, key)) return "System Whitecube config file";
	return "Built-in / input-device default";
}
static void page_advanced(void) {
	if (!wc_prefs_init()) { para("The Whitecube settings schema is missing. Reinstall the Whitecube bundle.", C_BAD); return; }
	para("Whitecube preferences. Changes apply live in the updated desktop. Reset removes only the selected override and restores the inherited config or device default.", NULL);
	textfield(&advanced.search, "Search by name, key or description", ui.cw, false); row_end();
	if (strcmp(advanced.search.buf, advanced.last_search)) {
		advanced.offset = 0; copy(advanced.last_search, sizeof(advanced.last_search), advanced.search.buf);
	}
	if (toggle("Only my overrides", advanced.modified_only, true)) { advanced.modified_only = !advanced.modified_only; advanced.offset = 0; }
	row_end();
	gsize count = g_strv_length(wc_pref_keys);
	qsort(wc_pref_keys, count, sizeof(char *), pref_compare);
	int matches = 0;
	for (char **key = wc_pref_keys; *key; key++) {
		GSettingsSchemaKey *sk = g_settings_schema_get_key(wc_schema, *key);
		const char *title = g_settings_schema_key_get_summary(sk), *description = g_settings_schema_key_get_description(sk);
		GVariant *override = g_settings_get_user_value(wc_prefs, *key);
		bool match = (!advanced.modified_only || override) && (!advanced.search.buf[0] ||
			strcasestr(*key, advanced.search.buf) || strcasestr(title, advanced.search.buf) || strcasestr(description, advanced.search.buf));
		if (match && matches++ >= advanced.offset && matches <= advanced.offset + 6) {
			if (list_row(title, *key, !strcmp(*key, advanced.selected), NULL)) {
				copy(advanced.selected, sizeof(advanced.selected), *key);
				advanced.value.buf[0] = advanced.last_value[0] = 0;
			}
		}
		if (override) g_variant_unref(override);
		g_settings_schema_key_unref(sk);
	}
	char total[80]; snprintf(total, sizeof(total), "%d preferences · showing %d–%d", matches,
		matches ? advanced.offset + 1 : 0, matches < advanced.offset + 6 ? matches : advanced.offset + 6);
	para(total, NULL);
	if (button_ex("Previous", false, false, advanced.offset > 0)) advanced.offset = advanced.offset < 6 ? 0 : advanced.offset - 6;
	if (button_ex("Next", false, false, matches > advanced.offset + 6)) advanced.offset += 6;
	row_end();
	if (!advanced.selected[0]) { para("Select a preference to edit its value or reset it.", NULL); return; }
	GSettingsSchemaKey *sk = g_settings_schema_get_key(wc_schema, advanced.selected);
	char config[64]; copy(config, sizeof(config), advanced.selected);
	for (char *p = config; *p; p++) if (*p == '-') *p = '_';
	GVariant *def = g_settings_schema_key_get_default_value(sk);
	char *fallback = wc_pref_text(def);
	const char *effective = cfg_get(config, fallback);
	/* Follow external edits only while the field has no unsaved draft. */
	if (!strcmp(advanced.value.buf, advanced.last_value)) copy(advanced.value.buf, sizeof(advanced.value.buf), effective);
	copy(advanced.last_value, sizeof(advanced.last_value), effective);
	heading(g_settings_schema_key_get_summary(sk));
	para(g_settings_schema_key_get_description(sk), NULL);
	kv("Storage", "/org/whitecube/desktop/", NULL);
	kv("Key", advanced.selected, NULL);
	kv("Source", pref_origin(config), NULL);
	kv("Current value", effective[0] ? effective : "(empty)", NULL);
	bool writable = g_settings_is_writable(wc_prefs, advanced.selected) && !any_action_running();
	bool is_bool = g_variant_is_of_type(def, G_VARIANT_TYPE_BOOLEAN);
	if (is_bool) {
		if (toggle("Enabled", !strcmp(effective, "1"), writable)) cfg_set(config, !strcmp(effective, "1") ? "0" : "1");
	} else {
		bool submit = textfield(&advanced.value, "Value", ui.cw, false); row_end();
		if ((button_ex("Save value", true, false, writable && strcmp(advanced.value.buf, effective)) || submit) && writable)
			cfg_set(config, advanced.value.buf);
	}
	GVariant *override = g_settings_get_user_value(wc_prefs, advanced.selected);
	if (button_ex("Use inherited value", false, false, writable && override != NULL)) {
		action("Reset preference", P_PREFS, 2, "reset", advanced.selected);
		copy(advanced.value.buf, sizeof(advanced.value.buf), effective);
	}
	row_end();
	if (!g_settings_is_writable(wc_prefs, advanced.selected)) para("This preference is locked by the administrator.", C_WARN);
	if (override) g_variant_unref(override);
	g_variant_unref(def); g_free(fallback); g_settings_schema_key_unref(sk);
}

static void page_sound(void) {
	heading("Output");
	status_line(T_SOUND);
	const char *vol = tv(T_SOUND, "sound", "volume");
	bool usable = *vol && strcmp(vol, "?") && strcmp(vol, "none");
	if (!st.vol_drag && usable) st.vol = atoi(vol);
	if (st.vol < 0) st.vol = 0;
	int before = st.vol;
	bool commit = slider("Volume", &st.vol, 0, 100, " %", usable);
	st.vol_drag = ui.active >= 0 && st.vol != before ? true : st.vol_drag && ui.active >= 0;
	if (commit) { char v[8]; snprintf(v, sizeof(v), "%d", st.vol); action("Volume", P_USER, 2, "volume", v); st.vol_drag = false; }
	bool muted = yes(tv(T_SOUND, "sound", "muted"));
	if (toggle("Muted", muted, usable)) action("Mute", P_USER, 1, "mute");
	row_end();
	kv("Controlled by", tv(T_SOUND, "sound", "backend"), NULL);
	heading("Sound system");
	const char *mode = cfg_get("audio", "alsa");
	bool pw = !strcmp(mode, "pipewire");
	if (list_row("ALSA direct", "no sound server; one app at a time", !pw, NULL) && pw) { if (cfg_set("audio", "alsa")) toast("ALSA direct from next login"); }
	if (list_row("PipeWire in the session", "mixing; needed for Firefox audio", pw, NULL) && !pw) { if (cfg_set("audio", "pipewire")) toast("PipeWire starts with whitecube from next login"); }
	para("PipeWire runs as your user inside the whitecube session (started by whitecube-session), never as a system service. Changes take effect at next login.", NULL);
	heading("Sound cards");
	for (int i = 0; i < tables[T_SOUND].n; i++) {
		struct row *r = &tables[T_SOUND].rows[i];
		if (!strcmp(r->f[0], "card")) { char k[32]; snprintf(k, sizeof(k), "Card %s (%s)", r->f[1], r->f[2]); kv(k, r->f[3], NULL); }
	}
}

static void page_power(void) {
	heading("Battery");
	status_line(T_POWER);
	for (int i = 0; i < tables[T_POWER].n; i++) {
		struct row *r = &tables[T_POWER].rows[i];
		if (!strcmp(r->f[0], "battery")) {
			int pct = atoi(r->f[2]);
			flow_newline();
			double w = fmin(ui.cw, 520 * S), h = 30 * S;
			frame_rect(ui.cx, ui.cy, w, h, C_INK, 1.5);
			rect(ui.cx + 4, ui.cy + 4, (w - 8) * pct / 100.0, h - 8, pct < 15 ? C_BAD : C_INK);
			char t[64]; snprintf(t, sizeof(t), "%d%%  %s", pct, r->f[3]);
			text(ui.cx + w + 14 * S, ui.cy + (h - F() * 1.25) / 2, t, F(), true, C_INK, 0);
			ui.cy += h + 10 * S;
			if (atof(r->f[5]) > 0) {
				char hl[64]; snprintf(hl, sizeof(hl), "%.0f%% of design capacity", 100 * atof(r->f[4]) / atof(r->f[5]));
				kv("Battery health", hl, NULL);
			}
		} else if (!strcmp(r->f[0], "ac")) kv("Charger", !strcmp(r->f[2], "online") ? "plugged in" : "unplugged", NULL);
	}
	heading("Screen");
	const char *b = "";
	for (int i = 0; i < tables[T_POWER].n; i++) if (!strcmp(tables[T_POWER].rows[i].f[0], "backlight")) b = tables[T_POWER].rows[i].f[2];
	if (!st.bright_drag && *b) st.bright = atoi(b);
	if (st.bright < 1) st.bright = 1;
	int before = st.bright;
	bool commit = slider("Brightness", &st.bright, 1, 100, " %", *b != 0);
	st.bright_drag = ui.active >= 0 && st.bright != before ? true : st.bright_drag && ui.active >= 0;
	if (commit) { char v[8]; snprintf(v, sizeof(v), "%d", st.bright); action("Brightness", P_FREE, 2, "brightness", v); st.bright_drag = false; }
	if (!*b && tables[T_POWER].loaded) para("No adjustable backlight found.", NULL);
	heading("Session");
	if (button("Lock screen")) spawn_detached("swaylock -f -c ffffff");
	if (button("Suspend")) action("Suspend", P_SUSPEND, 0);
	if (button("Restart…")) { struct pending p = make_pending("Restart", P_FREE, 2, "power-action", "reboot"); confirm("Restart", "Restart the computer now? Open windows will close.", "Restart", true, &p); }
	if (button_ex("Power off…", false, true, true)) { struct pending p = make_pending("Power off", P_FREE, 2, "power-action", "poweroff"); confirm("Power off", "Power off now? Disks are synced and unmounted cleanly first.", "Power off", true, &p); }
	row_end();
	para("Power off and restart go through systemd, so the encrypted disks are unmounted cleanly (no journal recovery at next boot).", NULL);
}

// ------------------------------------------------------------------ privacy

static const char *privacy_sections[] = {"Overview", "Desktop", "Activity", "Devices", "Browser"};
static const char *privacy_section_keys[] = {"overview", "desktop", "activity", "devices", "browser"};
static const char *privacy_detail(const char *kind, const char *key) {
	for (int i = 0; i < tables[T_PRIVACY].n; i++) {
		struct row *r = &tables[T_PRIVACY].rows[i];
		if (!strcmp(r->f[0], kind) && !strcmp(r->f[1], key)) return r->f[3];
	}
	return "";
}
// Explicit choices preserve the distinction between Off and unmanaged/unavailable.
static int privacy_choice(const char *label, const char *detail, const char *value, bool enabled) {
	flow_newline();
	bool known = !strcmp(value, "0") || !strcmp(value, "1");
	double available = ui.cw - 144 * S;
	bool compact = available < 260 * S;
	double title_h = text_ex(ui.cx, ui.cy + 7 * S, label, F(), true, false, C_INK,
		compact ? ui.cw : available, true, NULL);
	if (compact) ui.cy += title_h + 14 * S;
	ui.in_row = true; ui.row_x = compact ? ui.cx : ui.cx + ui.cw - 134 * S;
	ui.row_h = fmax(RH(), compact ? 0 : title_h + 7 * S);
	int selected = -1;
	if (button_ex("On", known && !strcmp(value, "1"), false, enabled)) selected = 1;
	if (button_ex("Off", known && !strcmp(value, "0"), false, enabled)) selected = 0;
	row_end();
	if (!known) {
		char state[160]; snprintf(state, sizeof(state), "Status: %s", *value ? value : "checking…");
		para(state, enabled ? C_MUTED : C_WARN);
	}
	para(detail, NULL);
	rect(ui.cx, ui.cy, ui.cw, 0.5, C_LINE); ui.cy += 10 * S;
	return selected;
}
static void privacy_native(const char *key, const char *label, const char *detail, const char *fallback) {
	int choice = privacy_choice(label, detail, cfg_boolean(key, fallback), !any_action_running());
	if (choice >= 0 && cfg_set(key, choice ? "1" : "0")) toast("Saving %s…", label);
}
static void privacy_external(const char *key, const char *label, const char *detail) {
	const char *state = tv(T_PRIVACY, "setting", key);
	bool ready = tables[T_PRIVACY].loaded && !tables[T_PRIVACY].loading && !tables[T_PRIVACY].err[0] &&
		*state && strcmp(state, "unsupported") && strcmp(state, "error") && !any_action_running();
	int choice = privacy_choice(label, detail, state, ready);
	if (!ready && *privacy_detail("setting", key)) para(privacy_detail("setting", key), C_MUTED);
	if (choice >= 0) action(label, P_PRIVACY, 3, "set", key, choice ? "1" : "0");
}
static void privacy_clear(const char *key, const char *label, const char *description) {
	bool ready = tables[T_PRIVACY].loaded && !tables[T_PRIVACY].loading && !tables[T_PRIVACY].err[0] &&
		!strcmp(tv(T_PRIVACY, "action", key), "available") && !any_action_running();
	if (button_ex(label, false, true, ready)) {
		struct pending p = make_pending(label, P_PRIVACY, 2, "clear", key);
		confirm(label, description, "Clear", true, &p);
	}
	row_end();
	para(privacy_detail("action", key), NULL);
}
static void page_privacy(void) {
	para("Choose what your desktop shows, remembers and shares.", NULL);
	for (int i = 0; i < 5; i++) {
		if (button_ex(privacy_sections[i], ui.privacy_section == i, false, true)) {
			ui.privacy_section = i; ui.scroll[PG_PRIVACY] = 0; ui.focus = -1; request_redraw();
		}
	}
	row_end();
	status_line(T_PRIVACY);
	switch (ui.privacy_section) {
	case 0:
		heading("Your privacy controls");
		para("15 preferences across your desktop, saved activity, microphone and Firefox. Changes are saved per user; clearing data always asks first.", NULL);
		if (list_row("Desktop visibility & locking", "7 controls", false, NULL)) { ui.privacy_section = 1; request_redraw(); }
		if (list_row("Activity & cleanup", "Recent files and caches", false, NULL)) { ui.privacy_section = 2; request_redraw(); }
		if (list_row("Microphone", "Capture mute", false, NULL)) { ui.privacy_section = 3; request_redraw(); }
		if (list_row("Firefox", "6 controls", false, NULL)) { ui.privacy_section = 4; request_redraw(); }
		heading("Current session");
		kv("Lock screen support", g.session_lock_available ? "Available in this session" : g.display ? "Restart Whitecube to enable the new lock support" : "Checked when opened on your desktop", g.session_lock_available ? C_GOOD : C_MUTED);
		kv("Title visibility", !strcmp(cfg_boolean("privacy_hide_titles", "0"), "1") ? "Application names only" : "Window titles visible", NULL);
		kv("Saved layouts", !strcmp(cfg_boolean("privacy_remember_layout", "1"), "1") ? "Remembered" : "Disabled", NULL);
		kv("Network names", !strcmp(cfg_boolean("privacy_hide_network", "0"), "1") ? "Hidden in the top bar" : "Visible", NULL);
		para("Desktop controls require the updated Whitecube session. Once running, changes are picked up automatically. Browser preferences take effect after Firefox restarts.", NULL);
		if (button("Refresh status")) refresh_page(true);
		if (button_ex("Lock now", false, false, g.session_lock_available)) spawn_detached("swaylock -f -c 090c12");
		row_end();
		break;
	case 1: {
		if (g.display && !g.session_lock_available) para("Log out and back in to activate the new compositor privacy controls. Your choices are saved now.", C_WARN);
		heading("Visible information");
		privacy_native("privacy_hide_titles", "Hide window titles", "Show application names in the tab bar instead of document, conversation and page titles.", "0");
		privacy_native("privacy_hide_network", "Hide network names", "Conceal connected and saved Wi-Fi names in the top bar and quick settings. Network settings still show names so you can manage connections.", "0");
		heading("Remembering your workspace");
		privacy_native("privacy_remember_layout", "Remember window layouts", "Save and restore window positions and workspace placement. Turning this off removes Whitecube's saved native layout.", "1");
		privacy_native("privacy_save_titles", "Save titles in layouts", "Include window titles when matching saved windows. Turning this off removes titles from stored layout records.", "1");
		heading("Clipboard & locking");
		privacy_native("privacy_clipboard", "Allow clipboard sharing", "Allow applications to provide the Wayland clipboard. Turning this off clears it and rejects new selections; apps can still copy within their own windows.", "1");
		privacy_native("privacy_clear_clipboard_on_lock", "Clear clipboard when locking", "Drop the compositor's clipboard selection when the screen locks. Applications may retain their own copies.", "1");
		para("Automatic screen lock after no keyboard or pointer activity:", C_INK);
		const int minutes[] = {0, 1, 5, 10, 15, 30};
		int current = atoi(cfg_get("privacy_lock_minutes", "0"));
		for (unsigned i = 0; i < sizeof(minutes) / sizeof(minutes[0]); i++) {
			char name[24], value[16];
			if (minutes[i]) snprintf(name, sizeof(name), "%d min", minutes[i]); else copy(name, sizeof(name), "Never");
			if (button_ex(name, current == minutes[i], false, true)) {
				snprintf(value, sizeof(value), "%d", minutes[i]);
				if (cfg_set("privacy_lock_minutes", value)) toast("Saving automatic lock: %s…", name);
			}
		}
		row_end();
		para("Automatic lock needs the updated compositor and swaylock. Locking protects this graphical session; console logins have their own access rules.", NULL);
		break;
	}
	case 2:
		heading("Recent activity");
		privacy_external("remember_recent", "Remember recent files", "Allow GTK 3 and GTK 4 applications to record recently opened files. Restart those applications after changing this. Existing history is kept until you clear it.");
		heading("Clear local data");
		para("These actions remove only the selected local records. They do not delete your documents or browser history.", NULL);
		privacy_clear("recent", "Clear recent file history…", "Remove this account's recently-used file list? Your documents are kept. Open apps may create new records.");
		privacy_clear("thumbnails", "Clear thumbnail cache…", "Remove cached image thumbnails? Original files are kept and previews may be generated again.");
		privacy_clear("clipboard", "Clear clipboard…", "Clear the current Wayland clipboard? Applications may keep their own copies of previously copied data.");
		break;
	case 3:
		heading("Microphone");
		privacy_external("microphone_muted", "Mute microphone capture", "Mute the default PipeWire microphone or the detected ALSA capture controls. This is an audio mute, not a per-application permission or hardware disconnect.");
		para(privacy_detail("setting", "microphone_muted"), NULL);
		para("The current capture state is checked while this page is open. Other applications can change audio controls. Separate devices may have their own mute switches.", NULL);
		break;
	case 4:
		heading("Firefox preferences");
		para(privacy_detail("scope", "firefox"), NULL);
		para("Applied to your detected default Firefox profile(s). Restart Firefox after changes. Unmanaged means Whitecube has not configured that preference; it is not a measurement of the browser's live state.", NULL);
		privacy_external("firefox_telemetry", "Send Firefox telemetry", "Allow Firefox telemetry and health-report uploads. Turn off to write explicit preferences disabling the supported reporting features.");
		privacy_external("firefox_https_only", "HTTPS-only browsing", "Require secure connections in normal and private windows. Firefox can ask before making an exception for an HTTP site.");
		privacy_external("firefox_tracking_strict", "Strict tracking protection", "Use Firefox's strict tracking-protection preset. Some websites may need an exception in Firefox.");
		privacy_external("firefox_suggestions", "Online search suggestions", "Send typed search text for suggestions from your search provider. Turn off to disable search suggestions in the search and address bars.");
		privacy_external("firefox_prefetch", "Speculative connections", "Allow Firefox to predictively fetch pages, DNS records and connections before you follow links.");
		privacy_external("firefox_session_restore", "Restore browser session", "Reopen the previous browser session at startup. Turning this off selects the home page and disables automatic recovery after a crash; it does not erase session files.");
		break;
	}
}

static void page_firewall(void) {
	heading("Firewall (ufw)");
	status_line(T_FW);
	const char *s = tv(T_FW, "fw", "status");
	bool active = !strcmp(s, "active");
	kv("Status", *s ? (active ? "on" : "off") : "", active ? C_GOOD : C_BAD);
	kv("Defaults", tv(T_FW, "fw", "default"), NULL);
	if (tables[T_FW].loaded && !active && button_ex("Turn firewall on", true, false, true))
		action("Firewall on", P_PASS, 2, "firewall-set", "enable");
	if (active && button_ex("Turn firewall off…", false, true, true)) {
		struct pending p = make_pending("Firewall off", P_PASS, 2, "firewall-set", "disable");
		confirm("Turn the firewall off", "Incoming connections will no longer be filtered.", "Turn off", true, &p);
	}
	if (button("Deny incoming by default")) action("Deny incoming", P_PASS, 2, "firewall-set", "deny-incoming");
	if (button("Allow incoming by default")) {
		struct pending p = make_pending("Allow incoming", P_PASS, 2, "firewall-set", "allow-incoming");
		confirm("Allow incoming", "Every listening service becomes reachable from the network.", "Allow", true, &p);
	}
	row_end();
	heading("Rules");
	int n = 0;
	for (int i = 0; i < tables[T_FW].n; i++) if (!strcmp(tables[T_FW].rows[i].f[0], "rule")) { para(tables[T_FW].rows[i].f[1], C_INK); n++; }
	if (!n && tables[T_FW].loaded) para("No rules. With incoming denied, nothing can connect in.", NULL);
}

static void page_services(void) {
	heading("System services");
	para("Changes need your password. Services marked protected keep the desktop running and are shown for information only.", NULL);
	status_line(T_SERVICES);
	for (int i = 0; i < tables[T_SERVICES].n; i++) {
		struct row *r = &tables[T_SERVICES].rows[i];
		if (strcmp(r->f[0], "service")) continue;
		bool enabled = !strcmp(r->f[3], "enabled"), running = !strcmp(r->f[4], "active"), prot = !strcmp(r->f[5], "protected");
		bool masked = !strncmp(r->f[3], "masked", 6);
		flow_newline();
		rect(ui.cx, ui.cy - 4 * S, ui.cw, 1, C_LINE);
		ui.cy += 4 * S;
		text(ui.cx, ui.cy, r->f[2], F(), true, C_INK, ui.cw * .45);
		text(ui.cx, ui.cy + 20 * S, r->f[1], 12 * S, false, C_MUTED, ui.cw * .45);
		char badge[64]; snprintf(badge, sizeof(badge), "%s · %s", r->f[3], *r->f[4] ? r->f[4] : "unknown");
		text(ui.cx + ui.cw * .46, ui.cy + 8 * S, badge, 13 * S, false, running ? C_GOOD : C_MUTED, ui.cw * .2);
		double save_cx = ui.cx, save_cw = ui.cw;
		ui.cx = save_cx + ui.cw * .67; ui.cw = save_cw * .33;
		ui.in_row = true; ui.row_x = ui.cx; ui.row_h = 0;
		if (prot) { text(ui.cx, ui.cy + 8 * S, "protected", 13 * S, false, C_MUTED, 0); ui.row_h = 40 * S; }
		else if (masked) {
			if (button("Unmask")) action("Unmask service", P_PASS, 3, "service", "unmask", r->f[1]);
		} else {
			if (button(running ? "Stop" : "Start")) action(running ? "Stop service" : "Start service", P_PASS, 3, "service", running ? "stop" : "start", r->f[1]);
			if (button(enabled ? "Disable" : "Enable")) action(enabled ? "Disable service" : "Enable service", P_PASS, 3, "service", enabled ? "disable" : "enable", r->f[1]);
		}
		ui.row_h = fmax(ui.row_h, 40 * S);
		row_end();
		ui.cx = save_cx; ui.cw = save_cw;
	}
}

static void page_users(void) {
	heading("Accounts");
	status_line(T_USERS);
	const char *groups = "";
	for (int i = 0; i < tables[T_USERS].n; i++) if (!strcmp(tables[T_USERS].rows[i].f[0], "groups")) groups = tables[T_USERS].rows[i].f[1];
	const char *me = getenv("USER") ? getenv("USER") : "";
	if (!st.selected_user[0]) copy(st.selected_user, sizeof(st.selected_user), me);
	for (int i = 0; i < tables[T_USERS].n; i++) {
		struct row *r = &tables[T_USERS].rows[i];
		if (strcmp(r->f[0], "user")) continue;
		char title[160]; snprintf(title, sizeof(title), "%s%s", r->f[1], !strcmp(r->f[1], me) ? "  (you)" : "");
		char detail[96]; snprintf(detail, sizeof(detail), "uid %s · %s", r->f[2], r->f[4]);
		if (list_row(title, detail, !strcmp(st.selected_user, r->f[1]), NULL)) copy(st.selected_user, sizeof(st.selected_user), r->f[1]);
		if (strcmp(st.selected_user, r->f[1]) || atoi(r->f[2]) < 1000) continue;
		spacer(6);
		para("Group membership (takes effect at next login):", NULL);
		char gl[256]; copy(gl, sizeof(gl), groups);
		for (char *save = NULL, *gname = strtok_r(gl, " ", &save); gname; gname = strtok_r(NULL, " ", &save)) {
			char mine[512]; snprintf(mine, sizeof(mine), " %s ", r->f[5]);
			char needle[64]; snprintf(needle, sizeof(needle), " %s ", gname);
			bool member = strstr(mine, needle) != NULL;
			if (toggle(gname, member, true)) action(member ? "Leave group" : "Join group", P_PASS, 4, "group", member ? "remove" : "add", r->f[1], gname);
		}
		row_end();
		spacer(6);
	}
	heading("Password");
	if (button("Change my password…")) spawn_detached("/usr/bin/whitecube-terminal -T 'Change password' sh -c 'passwd; printf \"\\nPress Enter to close\"; read -r _'");
	row_end();
}

static void page_updates(void) {
	heading("Software updates");
	status_line(T_UPDATES);
	if (tables[T_UPDATES].loading) para("Checking…", NULL);
	kv("Package lists refreshed", tv(T_UPDATES, "updates", "refreshed"), NULL);
	const char *count = tv(T_UPDATES, "updates", "count");
	kv("Updates available", count, atoi(count) ? C_WARN : C_GOOD);
	if (button_ex("Update now…", true, false, true))
		spawn_detached("/usr/bin/whitecube-terminal -T 'System update' sh -c 'sudo apt update && sudo apt full-upgrade; printf \"\\nPress Enter to close\"; read -r _'");
	if (button("Check again")) load(T_UPDATES, P_USER, 1, "updates");
	row_end();
	para("Updates run in a terminal so you can read and answer apt's questions. The package lists shown here are as of the last refresh.", NULL);
	char list[4096] = ""; int n = 0;
	for (int i = 0; i < tables[T_UPDATES].n; i++) {
		struct row *r = &tables[T_UPDATES].rows[i];
		if (strcmp(r->f[0], "package")) continue;
		if (n++) strncat(list, ",  ", sizeof(list) - strlen(list) - 1);
		strncat(list, r->f[1], sizeof(list) - strlen(list) - 1);
	}
	if (n) { heading("Packages"); para(list, C_INK); }
}

static void page_system(void) {
	heading("This computer");
	status_line(T_SYS);
	if (!st.host_init && tables[T_SYS].loaded) { copy(st.host.buf, sizeof(st.host.buf), tv(T_SYS, "sys", "hostname")); st.host_init = true; }
	flow_newline();
	text(ui.cx, ui.cy + (RH() - F() * 1.25) / 2, "Hostname", F(), false, C_MUTED, 0);
	ui.in_row = true; ui.row_x = ui.cx + 190 * S; ui.row_h = RH();
	bool set = textfield(&st.host, "hostname", 240 * S, false);
	set |= button_ex("Rename", false, false, st.host.buf[0] && strcmp(st.host.buf, tv(T_SYS, "sys", "hostname")));
	if (set) action("Rename computer", P_PASS, 2, "hostname", st.host.buf);
	row_end();
	kv("System", tv(T_SYS, "sys", "os"), NULL);
	kv("Kernel", tv(T_SYS, "sys", "kernel"), NULL);
	kv("Processor", tv(T_SYS, "sys", "cpu"), NULL);
	kv("Memory", tv(T_SYS, "sys", "memory"), NULL);
	kv("Up for", tv(T_SYS, "sys", "uptime"), NULL);

	heading("Date and time");
	kv("Now", tv(T_SYS, "sys", "time"), NULL);
	kv("Time zone", tv(T_SYS, "sys", "timezone"), NULL);
	const char *sync = tv(T_SYS, "sys", "time_sync");
	kv("Clock sync (chrony)", sync, !strcmp(sync, "Normal") ? C_GOOD : C_WARN);
	flow_newline();
	textfield(&st.tz_filter, "Find a time zone, e.g. Toronto", fmin(ui.cw, 420 * S), false);
	row_end();
	if (st.tz_filter.buf[0]) {
		int shown = 0;
		for (int i = 0; i < tables[T_ZONES].n && shown < 10; i++) {
			const char *z = tables[T_ZONES].rows[i].f[1];
			if (!strcasestr(z, st.tz_filter.buf)) continue;
			shown++;
			if (list_row(z, "", !strcmp(z, tv(T_SYS, "sys", "timezone")), NULL)) action("Time zone", P_PASS, 2, "timezone", z);
		}
		if (!shown) para("No matching time zone.", NULL);
	}

	heading("Disks");
	status_line(T_DISKS);
	for (int i = 0; i < tables[T_DISKS].n; i++) {
		struct row *r = &tables[T_DISKS].rows[i];
		if (strcmp(r->f[0], "disk") || !strcmp(r->f[2], "loop") || !strcmp(r->f[2], "rom")) continue;
		char d[256]; snprintf(d, sizeof(d), "%s  %s  %s%s%s%s%s", r->f[3], r->f[4], r->f[5], *r->f[6] ? "  on " : "", r->f[6],
			*r->f[7] ? "  " : "", *r->f[7] ? r->f[7] : "");
		char k[96]; snprintf(k, sizeof(k), "%s (%s)", r->f[1], !strcmp(r->f[2], "crypt") ? "unlocked" : r->f[2]);
		kv(k, d, NULL);
	}
}

// ---------------------------------------------------------------- frame

static void draw_modal(double W, double H) {
	ui.modal_first = ui.idc + 1;
	cairo_set_source_rgba(ui.cr, 0, 0, 0, .35); cairo_paint(ui.cr);
	double w = fmin(W - 40, 560 * S), h = 250 * S, x = (W - w) / 2, y = (H - h) / 2;
	rect(x, y, w, h, C_BG); frame_rect(x, y, w, h, C_INK, 2);
	double save_cx = ui.cx, save_cy = ui.cy, save_cw = ui.cw, save_top = ui.view_top, save_vh = ui.view_h;
	ui.cx = x + 24 * S; ui.cy = y + 20 * S; ui.cw = w - 48 * S; ui.in_row = false; ui.view_top = 0; ui.view_h = H;
	ui.cy += text(ui.cx, ui.cy, modal.title, 18 * S, true, C_INK, ui.cw) + 8 * S;
	ui.cy += text_ex(ui.cx, ui.cy, modal.text, 13 * S, false, false, C_MUTED, ui.cw, true, NULL) + 14 * S;
	bool submit = false;
	if (modal.kind == M_PASSWORD) { submit |= textfield(&modal.a, "Password", ui.cw, true); row_end(); }
	if (modal.kind == M_ADD_WIFI) {
		submit |= textfield(&modal.a, "Network name (SSID)", ui.cw, false); row_end();
		submit |= textfield(&modal.b, "Passphrase (8-63 characters)", ui.cw, true); row_end();
	}
	ui.cy = y + h - RH() - 20 * S;
	bool ok = button_ex(modal.ok_label, true, modal.danger, true) || submit;
	bool cancel = button("Cancel");
	row_end();
	if (ok) {
		if (modal.kind == M_PASSWORD) {
			copy(g_password, sizeof(g_password), modal.a.buf); g_have_password = true;
			explicit_bzero(modal.a.buf, sizeof(modal.a.buf));
			modal.kind = M_NONE; job_start_pending(&modal.pending);
		} else if (modal.kind == M_CONFIRM) {
			modal.kind = M_NONE; run_pending(&modal.pending);
		} else if (modal.kind == M_ADD_WIFI) {
			size_t pl = strlen(modal.b.buf);
			if (!modal.a.buf[0] || strlen(modal.a.buf) > 32) toast("The network name must be 1-32 characters");
			else if (pl < 8 || pl > 63) toast("The passphrase must be 8-63 characters");
			else {
				struct pending p = make_pending("Save network", P_PASS, 1, "net-add");
				p.chain = C_CONNECT_ADDED;
				snprintf(p.input, sizeof(p.input), "%s\n%s\n", modal.a.buf, modal.b.buf);
				explicit_bzero(modal.b.buf, sizeof(modal.b.buf));
				modal.kind = M_NONE; run_pending(&p);
			}
		}
		ui.focus = -1;
	}
	if (cancel) {
		explicit_bzero(modal.a.buf, sizeof(modal.a.buf)); explicit_bzero(modal.b.buf, sizeof(modal.b.buf));
		explicit_bzero(&modal.pending, sizeof(modal.pending));
		modal.kind = M_NONE; ui.focus = -1;
	}
	ui.cx = save_cx; ui.cy = save_cy; ui.cw = save_cw; ui.view_top = save_top; ui.view_h = save_vh;
}

static void draw(cairo_t *cr, double W, double H) {
	ui.cr = cr; ui.idc = 0; ui.nhits = 0; ui.edit = NULL; ui.modal_first = 1 << 30;
	settings_palette();
	rect(0, 0, W, H, C_BG);
	// Sidebar
	double sw = 210 * S;
	rect(0, 0, sw, H, C_SIDE);
	rect(sw - 1.5, 0, 1.5, H, C_INK);
	double y = 18 * S;
	text(18 * S, y, "SETTINGS", 13 * S, true, C_INK, 0);
	y += 34 * S;
	double side_top = y, side_bottom = H - 42 * S;
	double side_max = fmax(0, PG_COUNT * 36 * S - (side_bottom - side_top));
	if (ui.focus_visible && ui.focus >= 1 && ui.focus <= PG_COUNT) {
		double focused = (ui.focus - 1) * 36 * S;
		if (focused < ui.sidebar_scroll) ui.sidebar_scroll = focused;
		if (focused + 34 * S > ui.sidebar_scroll + side_bottom - side_top)
			ui.sidebar_scroll = focused + 34 * S - (side_bottom - side_top);
	}
	ui.sidebar_scroll = fmax(0, fmin(ui.sidebar_scroll, side_max));
	cairo_save(cr); cairo_rectangle(cr, 0, side_top, sw, side_bottom - side_top); cairo_clip(cr);
	y -= ui.sidebar_scroll;
	for (int p = 0; p < PG_COUNT; p++) {
		double h = 34 * S;
		int id = add_hit(0, y, sw - 2, h, H_SIDEBAR);
		bool sel = (int)ui.page == p;
		if (sel) rect(8 * S, y, sw - 16 * S, h, C_INK);
		cube(20 * S, y + (h - 12 * S) / 2, 12 * S, sel);
		if (sel) frame_rect(20 * S, y + (h - 12 * S) / 2, 12 * S, 12 * S, C_WHITE3, 1.5);
		text(42 * S, y + (h - 14 * S * 1.25) / 2, page_names[p], 14 * S, sel, sel ? C_WHITE3 : C_INK, 0);
		focus_ring(8 * S, y, sw - 16 * S, h, id);
		if (ui.clicked == id) {
			ui.clicked = -1;
			if ((int)ui.page != p) { ui.page = p; ui.focus = -1; refresh_page(false); }
		}
		y += h + 2 * S;
	}
	cairo_restore(cr);
	if (side_max > 0) {
		double track = side_bottom - side_top, thumb = track * track / (PG_COUNT * 36 * S);
		rect(sw - 5 * S, side_top + (track - thumb) * ui.sidebar_scroll / side_max, 2 * S, thumb, C_MUTED);
	}
	text(18 * S, H - 25 * S, "Ctrl+PgUp / PgDn", 10 * S, false, C_MUTED, sw - 30 * S);
	int busy = 0;
	for (int i = 0; i < MAXJOBS; i++) busy += jobs[i].used && jobs[i].table < 0;
	if (busy) text(18 * S, H - 42 * S, "Working…", 13 * S, false, C_MUTED, sw - 30 * S);

	// Content (scrolled, clipped)
	double top = 0, left = sw + 32 * S;
	ui.view_top = top; ui.view_h = H;
	cairo_save(cr);
	cairo_rectangle(cr, sw, 0, W - sw, H); cairo_clip(cr);
	double scroll = ui.scroll[ui.page];
	ui.cx = left; ui.cw = fmin(W - left - 32 * S, 900 * S); ui.cy = 24 * S - scroll; ui.in_row = false;
	ui.cy += text(ui.cx, ui.cy, page_names[ui.page], 26 * S, true, C_INK, 0) + 6 * S;
	switch (ui.page) {
	case PG_NETWORK: page_network(); break;
	case PG_DISPLAY: page_display(); break;
	case PG_APPEARANCE: page_appearance(); break;
	case PG_KEYBOARD: page_keyboard(); break;
	case PG_SOUND: page_sound(); break;
	case PG_POWER: page_power(); break;
	case PG_PRIVACY: page_privacy(); break;
	case PG_FIREWALL: page_firewall(); break;
	case PG_SERVICES: page_services(); break;
	case PG_USERS: page_users(); break;
	case PG_UPDATES: page_updates(); break;
	case PG_SYSTEM: page_system(); break;
	case PG_POINTER: page_pointer(); break;
	case PG_ADVANCED: page_advanced(); break;
	default: break;
	}
	flow_newline();
	ui.content_h = ui.cy + scroll + 40 * S;
	cairo_restore(cr);
	double max_scroll = fmax(0, ui.content_h - H);
	if (ui.scroll[ui.page] > max_scroll) { ui.scroll[ui.page] = max_scroll; request_redraw(); }
	if (max_scroll > 0) { // scroll indicator
		double th = H * H / ui.content_h, ty = (H - th) * ui.scroll[ui.page] / max_scroll;
		rect(W - 5 * S, ty, 3 * S, th, C_LINE);
	}
	// Toast
	if (ui.toast[0] && now_s() < ui.toast_until) {
		double tw = fmin(text_w(ui.toast, 14 * S, false) + 32 * S, W - sw - 40 * S), th = 40 * S;
		double tx = sw + (W - sw - tw) / 2, ty = H - th - 20 * S;
		rect(tx, ty, tw, th, C_INK);
		text(tx + 16 * S, ty + (th - 14 * S * 1.25) / 2, ui.toast, 14 * S, false, C_WHITE3, tw - 32 * S);
	}
	if (modal.kind != M_NONE) draw_modal(W, H);
	ui.clicked = ui.submit = ui.released = -1; ui.key_step = 0;
}

static void redraw(void) {
	if (!g.configured || g.closed) return;
	struct buffer *b = get_buffer();
	if (!b) return; // both busy; the release will trigger another frame
	cairo_surface_t *cs = cairo_image_surface_create_for_data(b->data, CAIRO_FORMAT_ARGB32, g.w, g.h, g.w * 4);
	cairo_t *cr = cairo_create(cs);
	g.dirty = false;
	draw(cr, g.w, g.h);
	// A click consumed by a widget changes state drawn earlier in this frame: draw once more.
	if (g.dirty) { g.dirty = false; draw(cr, g.w, g.h); }
	cairo_destroy(cr); cairo_surface_destroy(cs);
	wl_surface_attach(g.surface, b->wb, 0, 0);
	wl_surface_damage_buffer(g.surface, 0, 0, g.w, g.h);
	wl_surface_commit(g.surface);
	b->busy = true;
}

// ------------------------------------------------------------------ input

static struct hit *hit_at(double x, double y) {
	for (int i = ui.nhits - 1; i >= 0; i--) {
		struct hit *h = &ui.hits[i];
		if (modal.kind != M_NONE && h->id < ui.modal_first) continue;
		if (modal.kind == M_NONE && h->kind == H_SIDEBAR && (y < 52 * S || y >= g.h - 42 * S)) continue;
		if (modal.kind == M_NONE && h->kind != H_SIDEBAR && (x < 210 * S || y < 0 || y >= g.h)) continue;
		if (x >= h->x && x < h->x + h->w && y >= h->y && y < h->y + h->h) return h;
	}
	return NULL;
}
static void focus_move(int dir) {
	int best = -1;
	int lo = modal.kind != M_NONE ? ui.modal_first : 1;
	int ids[MAXHITS], n = 0;
	for (int i = 0; i < ui.nhits; i++) if (ui.hits[i].id >= lo) ids[n++] = ui.hits[i].id;
	if (!n) return;
	// hits are recorded in id order
	int cur = -1;
	for (int i = 0; i < n; i++) if (ids[i] == ui.focus) cur = i;
	best = cur < 0 ? (dir > 0 ? 0 : n - 1) : (cur + dir + n) % n;
	ui.focus = ids[best]; ui.focus_visible = true;
	// keep the focused widget in view
	for (int i = 0; i < ui.nhits; i++) if (ui.hits[i].id == ui.focus && ui.hits[i].kind != H_SIDEBAR && modal.kind == M_NONE) {
		struct hit *h = &ui.hits[i];
		if (h->y < 0) ui.scroll[ui.page] += h->y - 20 * S;
		else if (h->y + h->h > g.h) ui.scroll[ui.page] += h->y + h->h - g.h + 20 * S;
		if (ui.scroll[ui.page] < 0) ui.scroll[ui.page] = 0;
	}
}
static struct hit *focused_hit(void) {
	for (int i = 0; i < ui.nhits; i++) if (ui.hits[i].id == ui.focus) return &ui.hits[i];
	return NULL;
}
static void key_press(xkb_keysym_t sym, const char *utf8, bool ctrl, bool shift) {
	struct hit *fh = focused_hit();
	bool in_field = fh && fh->kind == H_FIELD && ui.edit;
	if (sym == XKB_KEY_Escape) {
		if (modal.kind != M_NONE) {
			explicit_bzero(modal.a.buf, sizeof(modal.a.buf)); explicit_bzero(modal.b.buf, sizeof(modal.b.buf));
		explicit_bzero(&modal.pending, sizeof(modal.pending));
			modal.kind = M_NONE; ui.focus = -1;
		} else if (ui.focus >= 0) ui.focus = -1;
		else g.closed = true;
	} else if (sym == XKB_KEY_Tab || sym == XKB_KEY_ISO_Left_Tab) focus_move(shift || sym == XKB_KEY_ISO_Left_Tab ? -1 : 1);
	else if (modal.kind == M_NONE && ctrl && (sym == XKB_KEY_Page_Down || sym == XKB_KEY_Page_Up)) {
		ui.page = (ui.page + (sym == XKB_KEY_Page_Down ? 1 : PG_COUNT - 1)) % PG_COUNT; ui.focus = -1; refresh_page(false);
	} else if (modal.kind == M_NONE && (sym == XKB_KEY_Page_Down || sym == XKB_KEY_Page_Up)) {
		ui.scroll[ui.page] += (sym == XKB_KEY_Page_Down ? 1 : -1) * g.h * .8;
		if (ui.scroll[ui.page] < 0) ui.scroll[ui.page] = 0;
	} else if (in_field) {
		struct field *f = ui.edit; size_t len = strlen(f->buf);
		if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter) ui.submit = ui.focus;
		else if (sym == XKB_KEY_BackSpace) {
			if (ctrl) f->buf[0] = 0;
			else { while (len > 0 && ((unsigned char)f->buf[len - 1] & 0xC0) == 0x80) len--; if (len > 0) len--; f->buf[len] = 0; }
		} else if (ctrl && (sym == XKB_KEY_u || sym == XKB_KEY_U)) f->buf[0] = 0;
		else if (utf8 && (unsigned char)utf8[0] >= 0x20 && utf8[0] != 0x7f && !ctrl) {
			size_t add = strlen(utf8);
			if (len + add < sizeof(f->buf)) memcpy(f->buf + len, utf8, add + 1);
		}
	} else if (sym == XKB_KEY_Return || sym == XKB_KEY_KP_Enter || sym == XKB_KEY_space) {
		if (fh) ui.clicked = ui.focus;
	} else if (fh && fh->kind == H_SLIDER && (sym == XKB_KEY_Left || sym == XKB_KEY_Right)) {
		ui.key_step = sym == XKB_KEY_Right ? 1 : -1;
	} else if (sym == XKB_KEY_Up || sym == XKB_KEY_Down) {
		if (modal.kind != M_NONE || (fh && fh->kind != H_SIDEBAR)) focus_move(sym == XKB_KEY_Down ? 1 : -1);
		else { ui.page = (ui.page + (sym == XKB_KEY_Down ? 1 : PG_COUNT - 1)) % PG_COUNT; refresh_page(false);
			ui.focus = ui.page + 1; ui.focus_visible = true; }
	}
	request_redraw();
}

static void stop_repeat(void);
static void kb_keymap(void *d, struct wl_keyboard *k, uint32_t format, int32_t fd, uint32_t size) {
	if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1) { close(fd); return; }
	char *map = mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
	close(fd);
	if (map == MAP_FAILED) return;
	struct xkb_keymap *km = xkb_keymap_new_from_string(g.xkb, map, XKB_KEYMAP_FORMAT_TEXT_V1, XKB_KEYMAP_COMPILE_NO_FLAGS);
	munmap(map, size);
	if (!km) return;
	stop_repeat();
	if (g.compose) xkb_compose_state_reset(g.compose);
	if (g.xstate) xkb_state_unref(g.xstate);
	if (g.keymap) xkb_keymap_unref(g.keymap);
	g.keymap = km; g.xstate = xkb_state_new(km);
}
static void kb_enter(void *d, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s, struct wl_array *keys) {}
static void stop_repeat(void) {
	g.repeat_key = 0;
	struct itimerspec off = {0};
	if (g.repeat_fd >= 0) timerfd_settime(g.repeat_fd, 0, &off, NULL);
}
static void kb_leave(void *d, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s) {
	stop_repeat();
	if (g.compose) xkb_compose_state_reset(g.compose);
}
static void handle_key(uint32_t key) {
	if (!g.xstate) return;
	xkb_keysym_t sym = xkb_state_key_get_one_sym(g.xstate, key + 8);
	char utf8[64] = "";
	xkb_state_key_get_utf8(g.xstate, key + 8, utf8, sizeof(utf8));
	bool ctrl = xkb_state_mod_name_is_active(g.xstate, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0;
	bool shift = xkb_state_mod_name_is_active(g.xstate, XKB_MOD_NAME_SHIFT, XKB_STATE_MODS_EFFECTIVE) > 0;
	struct hit *field = focused_hit();
	if (field && field->kind == H_FIELD && !ctrl && sym != XKB_KEY_Tab && sym != XKB_KEY_ISO_Left_Tab &&
	    sym != XKB_KEY_Escape && sym != XKB_KEY_Return && sym != XKB_KEY_BackSpace) {
		if (wc_compose_key(g.compose, sym, utf8, sizeof(utf8))) {
			if (!utf8[0]) { request_redraw(); return; }
			sym = XKB_KEY_NoSymbol;
		}
	} else if (g.compose) xkb_compose_state_reset(g.compose);
	key_press(sym, utf8, ctrl, shift);
}
static void kb_key(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t time, uint32_t key, uint32_t state) {
	if (state == WL_KEYBOARD_KEY_STATE_PRESSED) {
		stop_repeat();
		handle_key(key);
		if (g.compose && xkb_compose_state_get_status(g.compose) == XKB_COMPOSE_COMPOSING) return;
		if (g.keymap && xkb_keymap_key_repeats(g.keymap, key + 8) && g.repeat_rate > 0 && g.repeat_fd >= 0) {
			g.repeat_key = key;
			int delay = g.repeat_delay > 0 ? g.repeat_delay : 1;
			int rate = g.repeat_rate > 100 ? 100 : g.repeat_rate;
			struct itimerspec its = {
				.it_value = { delay / 1000, (delay % 1000) * 1000000L },
				.it_interval = { 0, 1000000000L / rate },
			};
			timerfd_settime(g.repeat_fd, 0, &its, NULL);
		}
	} else if (key == g.repeat_key) stop_repeat();
}
static void kb_mods(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t dep, uint32_t lat, uint32_t lock, uint32_t group) {
	if (g.xstate) xkb_state_update_mask(g.xstate, dep, lat, lock, 0, 0, group);
}
static void kb_repeat(void *d, struct wl_keyboard *k, int32_t rate, int32_t delay) { g.repeat_rate = rate; g.repeat_delay = delay; stop_repeat(); }
static const struct wl_keyboard_listener kb_listener = { kb_keymap, kb_enter, kb_leave, kb_key, kb_mods, kb_repeat };

static void set_cursor(uint32_t serial) {
	if (!g.cursor_theme) return;
	struct wl_cursor *c = wl_cursor_theme_get_cursor(g.cursor_theme, "left_ptr");
	if (!c) c = wl_cursor_theme_get_cursor(g.cursor_theme, "default");
	if (!c || !c->image_count) return;
	struct wl_cursor_image *img = c->images[0];
	wl_surface_attach(g.cursor_surface, wl_cursor_image_get_buffer(img), 0, 0);
	wl_surface_damage_buffer(g.cursor_surface, 0, 0, img->width, img->height);
	wl_surface_commit(g.cursor_surface);
	wl_pointer_set_cursor(g.ptr, serial, g.cursor_surface, img->hotspot_x, img->hotspot_y);
}
static void ptr_enter(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {
	g.px = wl_fixed_to_double(x); g.py = wl_fixed_to_double(y); set_cursor(serial);
}
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) {}
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
	g.px = wl_fixed_to_double(x); g.py = wl_fixed_to_double(y);
	if (ui.active >= 0) { ui.drag_x = g.px; request_redraw(); }
}
static void ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
	if (button != BTN_LEFT) return;
	if (state == WL_POINTER_BUTTON_STATE_PRESSED) {
		stop_repeat();
		if (g.compose) xkb_compose_state_reset(g.compose);
		struct hit *h = hit_at(g.px, g.py);
		ui.focus_visible = false;
		if (!h) { ui.focus = -1; request_redraw(); return; }
		ui.focus = h->id;
		if (h->kind == H_SLIDER) { ui.active = h->id; ui.drag_x = g.px; }
		else if (h->kind != H_FIELD) ui.clicked = h->id;
	} else if (ui.active >= 0) { ui.released = ui.active; ui.active = -1; }
	request_redraw();
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) {
	if (axis != WL_POINTER_AXIS_VERTICAL_SCROLL || modal.kind != M_NONE) return;
	if (g.px < 210 * S) ui.sidebar_scroll += wl_fixed_to_double(value) * 3;
	else ui.scroll[ui.page] += wl_fixed_to_double(value) * 3;
	if (ui.scroll[ui.page] < 0) ui.scroll[ui.page] = 0;
	request_redraw();
}
static void ptr_frame(void *d, struct wl_pointer *p) {}
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t s) {}
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) {}
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t v) {}
static void ptr_axis_value120(void *d, struct wl_pointer *p, uint32_t a, int32_t v) {}
static void ptr_axis_direction(void *d, struct wl_pointer *p, uint32_t a, uint32_t dir) {}
static const struct wl_pointer_listener ptr_listener = { ptr_enter, ptr_leave, ptr_motion, ptr_button, ptr_axis,
	ptr_frame, ptr_axis_source, ptr_axis_stop, ptr_axis_discrete, ptr_axis_value120, ptr_axis_direction };

static void seat_caps(void *d, struct wl_seat *s, uint32_t caps) {
	if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !g.kbd) { g.kbd = wl_seat_get_keyboard(s); wl_keyboard_add_listener(g.kbd, &kb_listener, NULL); }
	else if (!(caps & WL_SEAT_CAPABILITY_KEYBOARD) && g.kbd) {
		stop_repeat(); if (g.compose) xkb_compose_state_reset(g.compose);
		wl_keyboard_destroy(g.kbd); g.kbd = NULL;
	}
	if ((caps & WL_SEAT_CAPABILITY_POINTER) && !g.ptr) { g.ptr = wl_seat_get_pointer(s); wl_pointer_add_listener(g.ptr, &ptr_listener, NULL); }
}
static void seat_name(void *d, struct wl_seat *s, const char *n) {}
static const struct wl_seat_listener seat_listener = { seat_caps, seat_name };

static void wm_ping(void *d, struct xdg_wm_base *b, uint32_t serial) { xdg_wm_base_pong(b, serial); }
static const struct xdg_wm_base_listener wm_listener = { wm_ping };
static void xsurf_configure(void *d, struct xdg_surface *s, uint32_t serial) {
	xdg_surface_ack_configure(s, serial);
	if (g.pending_w > 0 && g.pending_h > 0) { g.w = g.pending_w; g.h = g.pending_h; }
	g.configured = true; request_redraw();
}
static const struct xdg_surface_listener xsurf_listener = { xsurf_configure };
static void top_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h, struct wl_array *states) { g.pending_w = w; g.pending_h = h; }
static void top_close(void *d, struct xdg_toplevel *t) { g.closed = true; }
static void top_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h) {}
static void top_caps(void *d, struct xdg_toplevel *t, struct wl_array *c) {}
static const struct xdg_toplevel_listener top_listener = { top_configure, top_close, top_bounds, top_caps };

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
	if (!strcmp(iface, "ext_session_lock_manager_v1")) g.session_lock_available = true;
	if (!strcmp(iface, wl_compositor_interface.name)) g.compositor = wl_registry_bind(r, name, &wl_compositor_interface, ver < 4 ? ver : 4);
	else if (!strcmp(iface, wl_shm_interface.name)) g.shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, xdg_wm_base_interface.name)) { g.wm_base = wl_registry_bind(r, name, &xdg_wm_base_interface, ver < 3 ? ver : 3); xdg_wm_base_add_listener(g.wm_base, &wm_listener, NULL); }
	else if (!g.render_only && !strcmp(iface, wl_seat_interface.name) && !g.seat) { g.seat = wl_registry_bind(r, name, &wl_seat_interface, ver < 5 ? ver : 5); wl_seat_add_listener(g.seat, &seat_listener, NULL); }
	else if (!strcmp(iface, zwlr_output_manager_v1_interface.name)) {
		om.mgr = wl_registry_bind(r, name, &zwlr_output_manager_v1_interface, ver < 4 ? ver : 4);
		zwlr_output_manager_v1_add_listener(om.mgr, &mgr_listener, NULL);
	}
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) {}
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

// ------------------------------------------------------------------ main

static int page_from_name(const char *n) {
	for (int i = 0; i < PG_COUNT; i++) if (!strcasecmp(n, page_keys[i])) return i;
	return -1;
}
static void pump_jobs(int timeout_ms) {
	struct pollfd fds[MAXJOBS * 2]; struct job *owner[MAXJOBS * 2]; int n = 0;
	for (int i = 0; i < MAXJOBS; i++) {
		if (!jobs[i].used) continue;
		if (jobs[i].out >= 0) { fds[n] = (struct pollfd){jobs[i].out, POLLIN, 0}; owner[n++] = &jobs[i]; }
		if (jobs[i].err >= 0) { fds[n] = (struct pollfd){jobs[i].err, POLLIN, 0}; owner[n++] = &jobs[i]; }
	}
	if (n && poll(fds, n, timeout_ms) > 0)
		for (int i = 0; i < n; i++) if (fds[i].revents && owner[i]->used) job_read(owner[i]);
	for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used) job_read(&jobs[i]);
}
static bool any_job(void) { for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used) return true; return false; }

// Draw one page to a PNG without a window: lets layouts be checked headless.
static int render_png(int page, const char *path, int W, int H) {
	g.render_only = true;
	if (getenv("WAYLAND_DISPLAY") && (g.display = wl_display_connect(NULL))) {
		g.registry = wl_display_get_registry(g.display);
		wl_registry_add_listener(g.registry, &reg_listener, NULL);
		wl_display_roundtrip(g.display); wl_display_roundtrip(g.display);
	}
	ui.page = page;
	refresh_page(true);
	if (page == PG_NETWORK) { st.scanned = false; }
	double deadline = now_s() + 25;
	while (any_job() && now_s() < deadline) pump_jobs(200);
	cairo_surface_t *cs = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H);
	cairo_t *cr = cairo_create(cs);
	g.h = H;
	draw(cr, W, H); // first pass loads anything the page asks for (variants, ...)
	while (any_job() && now_s() < deadline) pump_jobs(200);
	draw(cr, W, H);
	cairo_destroy(cr);
	cairo_status_t s = cairo_surface_write_to_png(cs, path);
	cairo_surface_destroy(cs);
	if (s != CAIRO_STATUS_SUCCESS) { fprintf(stderr, "cube-settings: cannot write %s\n", path); return 1; }
	return 0;
}

int main(int argc, char **argv) {
	setlocale(LC_CTYPE, "");
	signal(SIGPIPE, SIG_IGN);
	signal(SIGCHLD, SIG_DFL);
	find_privacy_helper();
	char exe[PATH_MAX]; ssize_t exe_n = readlink("/proc/self/exe", exe, sizeof(exe)-1);
	if (exe_n > 0) {
		exe[exe_n] = 0; char *parent = g_path_get_dirname(exe);
		snprintf(preferences_helper, sizeof(preferences_helper), "%s/cube-preferences", parent);
		if (access(preferences_helper, X_OK)) snprintf(preferences_helper, sizeof(preferences_helper), "%s/../cube-preferences", parent);
		g_free(parent);
	}
	if (access(preferences_helper, X_OK)) snprintf(preferences_helper, sizeof(preferences_helper), "%s/.local/bin/cube-preferences", home_dir());
	cfg_load(); acc_load();
	S = acc.ui_scale / 100.0;
	int start_page = 0;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--section") && i + 1 < argc) {
			const char *section = argv[++i]; bool found = false;
			for (int j = 0; j < 5; j++) if (!strcmp(section, privacy_section_keys[j])) { ui.privacy_section = j; found = true; }
			if (!found) { fprintf(stderr, "unknown privacy section %s\n", section); return 2; }
		}
		else if (!strcmp(argv[i], "--page") && i + 1 < argc) { int p = page_from_name(argv[++i]); if (p >= 0) start_page = p; else { fprintf(stderr, "unknown page %s\n", argv[i]); return 2; } }
		else if (!strcmp(argv[i], "--render") && i + 2 < argc) {
			int p = page_from_name(argv[i + 1]);
			if (p < 0) { fprintf(stderr, "unknown page %s\n", argv[i + 1]); return 2; }
			int W = 1100, H = 900;
			if (i + 3 < argc && sscanf(argv[i + 3], "%dx%d", &W, &H) != 2) { fprintf(stderr, "invalid render size\n"); return 2; }
			if (W < 640 || H < 400 || W > 4096 || H > 8192) { fprintf(stderr, "render size out of range\n"); return 2; }
			return render_png(p, argv[i + 2], W, H);
		} else {
			printf("usage: cube-settings [--page NAME] [--section overview|desktop|activity|devices|browser] | [--section NAME] --render NAME FILE.png [WxH]\npages:");
			for (int k = 0; k < PG_COUNT; k++) printf(" %s", page_keys[k]);
			printf("\n");
			return argv[i][1] == 'h' || !strcmp(argv[i], "--help") ? 0 : 2;
		}
	}
	ui.page = start_page;
	g.display = wl_display_connect(NULL);
	if (!g.display) { fprintf(stderr, "cube-settings: no Wayland display (run inside whitecube)\n"); return 1; }
	g.xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
	g.compose = wc_compose_create(g.xkb);
	g.registry = wl_display_get_registry(g.display);
	wl_registry_add_listener(g.registry, &reg_listener, NULL);
	wl_display_roundtrip(g.display);
	if (!g.compositor || !g.shm || !g.wm_base) { fprintf(stderr, "cube-settings: compositor lacks xdg-shell/shm\n"); return 1; }
	wl_display_roundtrip(g.display);
	g.cursor_theme = wl_cursor_theme_load(getenv("XCURSOR_THEME"), 24, g.shm);
	g.cursor_surface = wl_compositor_create_surface(g.compositor);
	g.surface = wl_compositor_create_surface(g.compositor);
	g.xsurf = xdg_wm_base_get_xdg_surface(g.wm_base, g.surface);
	xdg_surface_add_listener(g.xsurf, &xsurf_listener, NULL);
	g.top = xdg_surface_get_toplevel(g.xsurf);
	xdg_toplevel_add_listener(g.top, &top_listener, NULL);
	xdg_toplevel_set_app_id(g.top, "cube-settings");
	xdg_toplevel_set_title(g.top, "Whitecube Settings");
	xdg_toplevel_set_min_size(g.top, (int)(680 * S), (int)(440 * S));
	wl_surface_commit(g.surface);
	g.repeat_fd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	int tick_fd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK);
	struct itimerspec tick = { {2, 0}, {2, 0} };
	timerfd_settime(tick_fd, 0, &tick, NULL);
	refresh_page(true);

	while (!g.closed) {
		while (wl_display_prepare_read(g.display) != 0) wl_display_dispatch_pending(g.display);
		if (g.dirty) { redraw(); }
		wl_display_flush(g.display);
		struct pollfd fds[3 + MAXJOBS * 2]; struct job *owner[3 + MAXJOBS * 2] = {0}; int n = 0;
		fds[n++] = (struct pollfd){wl_display_get_fd(g.display), POLLIN, 0};
		fds[n++] = (struct pollfd){tick_fd, POLLIN, 0};
		fds[n++] = (struct pollfd){g.repeat_fd, POLLIN, 0};
		for (int i = 0; i < MAXJOBS; i++) {
			if (!jobs[i].used) continue;
			if (jobs[i].out >= 0) { fds[n] = (struct pollfd){jobs[i].out, POLLIN, 0}; owner[n++] = &jobs[i]; }
			if (jobs[i].err >= 0) { fds[n] = (struct pollfd){jobs[i].err, POLLIN, 0}; owner[n++] = &jobs[i]; }
		}
		if (poll(fds, n, -1) < 0 && errno != EINTR) { wl_display_cancel_read(g.display); break; }
		if (fds[0].revents & POLLIN) wl_display_read_events(g.display);
		else wl_display_cancel_read(g.display);
		if (fds[0].revents & (POLLERR | POLLHUP)) break;
		wl_display_dispatch_pending(g.display);
		uint64_t ticks;
		if (fds[1].revents & POLLIN && read(tick_fd, &ticks, sizeof(ticks)) > 0) {
			cfg_load();
			if (ui.active < 0) { acc_load(); S = acc.ui_scale / 100.0; }
			refresh_page(false); request_redraw();
			if (ui.toast[0] && now_s() >= ui.toast_until) { ui.toast[0] = 0; request_redraw(); }
		}
		if (fds[2].revents & POLLIN && read(g.repeat_fd, &ticks, sizeof(ticks)) > 0 && g.repeat_key)
			for (uint64_t i = 0; i < ticks && i < 8; i++) handle_key(g.repeat_key);
		for (int i = 3; i < n; i++) if (fds[i].revents && owner[i] && owner[i]->used) job_read(owner[i]);
		for (int i = 0; i < MAXJOBS; i++) if (jobs[i].used) job_read(&jobs[i]);
	}
	explicit_bzero(g_password, sizeof(g_password));
	(void)any_action_running;
	wl_display_disconnect(g.display);
	return 0;
}
