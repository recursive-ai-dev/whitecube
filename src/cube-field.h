// Native field controls; included by cube-ca.c to share its simulation and renderer.
// Runtime controls are private to the user and Wayland display, with no shell parsing.
static bool runtime_path(char *path, size_t size) {
	const char *runtime = getenv("XDG_RUNTIME_DIR"), *display = getenv("WAYLAND_DISPLAY");
	struct stat st;
	if (!runtime || stat(runtime, &st) || !S_ISDIR(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 0077)) return false;
	if (!display) display = "wayland-0";
	for (const char *p = display; *p; p++) if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') || *p == '-' || *p == '_')) return false;
	int n = snprintf(path, size, "%s/cube-ca-%s.sock", runtime, display);
	return n > 0 && (size_t)n < size;
}
static int send_control(const char *command) {
	struct sockaddr_un addr = {.sun_family = AF_UNIX}, reply = {.sun_family = AF_UNIX};
	if (!runtime_path(addr.sun_path, sizeof(addr.sun_path))) { fputs("cube-ca: private XDG_RUNTIME_DIR and WAYLAND_DISPLAY required\n", stderr); return 1; }
	int length = snprintf(reply.sun_path, sizeof(reply.sun_path), "%s/cube-ctl-%ld.sock", getenv("XDG_RUNTIME_DIR"), (long)getpid());
	if (length < 0 || (size_t)length >= sizeof(reply.sun_path)) return 1;
	int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
	if (fd < 0) { perror("cube-ca controls"); return 1; }
	mode_t old = umask(0077); int rc = bind(fd, (struct sockaddr *)&reply, sizeof(reply)); umask(old);
	if (rc) { perror("cube-ca control reply"); close(fd); return 1; }
	ssize_t n = sendto(fd, command, strlen(command), 0, (struct sockaddr *)&addr, sizeof(addr));
	if (n != (ssize_t)strlen(command)) { perror("cube-ca: wallpaper control socket"); close(fd); unlink(reply.sun_path); return 1; }
	struct pollfd event = {.fd=fd, .events=POLLIN}; char response[256]; rc = 1;
	if (poll(&event, 1, 3000) > 0 && (n = recv(fd, response, sizeof(response)-1, 0)) > 0) {
		response[n] = 0; puts(response); rc = strncmp(response, "OK ", 3) != 0;
	} else fputs("cube-ca: wallpaper did not acknowledge the command\n", stderr);
	close(fd); unlink(reply.sun_path); return rc;
}
static int open_control(struct ca *c) {
	struct sockaddr_un addr = {.sun_family = AF_UNIX};
	if (!runtime_path(addr.sun_path, sizeof(addr.sun_path))) return -1;
	int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
	if (fd < 0) { perror("cube-ca controls"); return -1; }
	struct stat st;
	if (!lstat(addr.sun_path, &st)) {
		if (!S_ISSOCK(st.st_mode) || st.st_uid != getuid()) { close(fd); return -1; }
		// Never replace another running instance's socket.
		if (!connect(fd, (struct sockaddr *)&addr, sizeof(addr)) || errno != ECONNREFUSED) { close(fd); return -1; }
		unlink(addr.sun_path); close(fd);
		fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
		if (fd < 0) return -1;
	}
	mode_t old = umask(0077);
	int rc = bind(fd, (struct sockaddr *)&addr, sizeof(addr)); umask(old);
	if (rc) { perror("cube-ca controls"); close(fd); return -1; }
	snprintf(c->control_path, sizeof(c->control_path), "%s", addr.sun_path);
	return fd;
}
static void update_input(struct ca *c) {
	if (!c->surf) return;
	if (c->edit) wl_surface_set_input_region(c->surf, NULL);
	else {
		struct wl_region *empty = wl_compositor_create_region(c->comp);
		wl_surface_set_input_region(c->surf, empty); wl_region_destroy(empty);
	}
	wl_surface_commit(c->surf);
}
static bool paint_cell(struct ca *c, double px, double py, int value) {
	int x, y, z = 0;
	if (c->volume) {
		if (c->slice < 0) return false;
		int cell = (c->W < c->H ? c->W : c->H) / 20;
		if (cell < 1) return false;
		int ox = (c->W - c->cols * cell) / 2, oy = (c->H - c->rows * cell) / 2;
		x = (int)floor((px - ox) / cell); y = (int)floor((py - oy) / cell); z = c->slice;
	} else {
		x = (int)floor(px / RULES[c->rule].cell); y = (int)floor(py / RULES[c->rule].cell);
	}
	if (x < 0 || y < 0 || x >= c->cols || y >= c->rows || z >= c->depth) return false;
	size_t i = ((size_t)z * c->rows + y) * c->cols + x;
	c->grid[i] = value != 0; c->age[i] = 0; c->manual = true;
	return true;
}
static void pointer_enter(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y) {
	struct ca *c = data; c->pointer_x = wl_fixed_to_double(x); c->pointer_y = wl_fixed_to_double(y);
}
static void pointer_leave(void *data, struct wl_pointer *p, uint32_t serial, struct wl_surface *s) { ((struct ca *)data)->draw_button = 0; }
static void pointer_motion(void *data, struct wl_pointer *p, uint32_t time, wl_fixed_t x, wl_fixed_t y) {
	struct ca *c = data; c->pointer_x = wl_fixed_to_double(x); c->pointer_y = wl_fixed_to_double(y);
	if (c->edit && c->draw_button) paint_cell(c, c->pointer_x, c->pointer_y, c->draw_button == BTN_LEFT);
}
static void pointer_button(void *data, struct wl_pointer *p, uint32_t serial, uint32_t time, uint32_t button, uint32_t state) {
	struct ca *c = data;
	if (!c->edit || (button != BTN_LEFT && button != BTN_RIGHT)) return;
	c->draw_button = state == WL_POINTER_BUTTON_STATE_PRESSED ? button : 0;
	if (c->draw_button) paint_cell(c, c->pointer_x, c->pointer_y, button == BTN_LEFT);
}
static void pointer_axis(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis, wl_fixed_t value) {
	struct ca *c = data;
	if (c->volume && c->edit && axis == WL_POINTER_AXIS_VERTICAL_SCROLL) {
		c->slice = (c->slice + (value > 0 ? 1 : c->depth - 1)) % c->depth;
	}
}
static void pointer_frame(void *data, struct wl_pointer *p) {}
static void pointer_axis_source(void *data, struct wl_pointer *p, uint32_t source) {}
static void pointer_axis_stop(void *data, struct wl_pointer *p, uint32_t time, uint32_t axis) {}
static void pointer_axis_discrete(void *data, struct wl_pointer *p, uint32_t axis, int32_t discrete) {}
static const struct wl_pointer_listener pointer_listener = {
	.enter = pointer_enter, .leave = pointer_leave, .motion = pointer_motion, .button = pointer_button,
	.axis = pointer_axis, .frame = pointer_frame, .axis_source = pointer_axis_source,
	.axis_stop = pointer_axis_stop, .axis_discrete = pointer_axis_discrete,
};
static void seat_capabilities(void *data, struct wl_seat *seat, uint32_t caps) {
	struct ca *c = data;
	if ((caps & WL_SEAT_CAPABILITY_POINTER) && !c->pointer) {
		c->pointer = wl_seat_get_pointer(seat); wl_pointer_add_listener(c->pointer, &pointer_listener, c);
	} else if (!(caps & WL_SEAT_CAPABILITY_POINTER) && c->pointer) { wl_pointer_destroy(c->pointer); c->pointer = NULL; }
}
static void seat_name(void *data, struct wl_seat *seat, const char *name) {}
static const struct wl_seat_listener seat_listener = { .capabilities = seat_capabilities, .name = seat_name };
static bool safe_seed_name(const char *name) {
	if (!*name || strlen(name) > 48) return false;
	for (; *name; name++) if (!((*name >= 'a' && *name <= 'z') || (*name >= 'A' && *name <= 'Z') || (*name >= '0' && *name <= '9') || *name == '-' || *name == '_')) return false;
	return true;
}
static bool seed_path(char *path, size_t size, const char *name) {
	if (!safe_seed_name(name)) return false;
	const char *state = getenv("XDG_STATE_HOME"), *home = getenv("HOME");
	if (!state && !home) return false;
	char dir[1024]; int n = state ? snprintf(dir, sizeof(dir), "%s/whitecube-ca", state) : snprintf(dir, sizeof(dir), "%s/.local/state/whitecube-ca", home);
	if (n < 0 || (size_t)n >= sizeof(dir) || dir[0] != '/') return false;
	for (char *p = dir + 1; *p; p++) if (*p == '/') { *p = 0; if (mkdir(dir, 0700) && errno != EEXIST) return false; *p = '/'; }
	if (mkdir(dir, 0700) && errno != EEXIST) return false;
	n = snprintf(path, size, "%s/%s.seed", dir, name);
	return n > 0 && (size_t)n < size;
}
static bool seed_save(struct ca *c, const char *name) {
	char path[1200], temp[1280]; if (!seed_path(path, sizeof(path), name)) return false;
	snprintf(temp, sizeof(temp), "%s.tmp-%ld", path, (long)getpid());
	int fd = open(temp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
	if (fd < 0) return false;
	FILE *f = fdopen(fd, "wb"); if (!f) { close(fd); unlink(temp); return false; }
	bool ok = fprintf(f, "WCUBE2 %d %d %d %u %u %.9f %d\n", c->cols, c->rows, c->depth, c->rule, c->seed, c->yaw, c->slice) > 0;
	ok = ok && fwrite(c->grid, 1, sim_count(c), f) == sim_count(c) && fwrite(c->age, 1, sim_count(c), f) == sim_count(c);
	if (fclose(f)) ok = false;
	if (ok && !rename(temp, path)) return true;
	unlink(temp); return false;
}
static bool seed_replay(struct ca *c, const char *name) {
	char path[1200]; if (!seed_path(path, sizeof(path), name)) return false;
	int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW); if (fd < 0) return false;
	FILE *f = fdopen(fd, "rb"); if (!f) { close(fd); return false; }
	struct ca saved = {0}; char line[256], extra; bool ok = fgets(line, sizeof(line), f) &&
		sscanf(line, "WCUBE2 %d %d %d %u %u %lf %d %c", &saved.cols, &saved.rows, &saved.depth, &saved.rule, &saved.seed, &saved.yaw, &saved.slice, &extra) == 7;
	ok = ok && saved.cols > 0 && saved.cols <= 1024 && saved.rows > 0 && saved.rows <= 1024 &&
		(saved.depth == 1 || (saved.depth == 18 && saved.cols == 18 && saved.rows == 18)) && saved.rule < NRULES && saved.seed && isfinite(saved.yaw) && saved.slice >= -1 && saved.slice < saved.depth;
	if (ok) ok = alloc_grid(&saved);
	if (ok) ok = fread(saved.grid, 1, sim_count(&saved), f) == sim_count(&saved) && fread(saved.age, 1, sim_count(&saved), f) == sim_count(&saved) && fgetc(f) == EOF;
	if (ok) for (size_t i = 0; i < sim_count(&saved); i++) if (saved.grid[i] > 1) { ok = false; break; }
	fclose(f);
	if (!ok) { free(saved.grid); free(saved.next); free(saved.age); return false; }
	free(c->grid); free(c->next); free(c->age);
	c->grid = saved.grid; c->next = saved.next; c->age = saved.age;
	c->cols = saved.cols; c->rows = saved.rows; c->depth = saved.depth; c->rule = saved.rule; c->seed = saved.seed;
	c->volume = saved.depth > 1; c->slice = saved.slice; c->yaw = saved.yaw;
	c->tick = 0; c->manual = true; c->paused = false; c->edit = false; update_input(c);
	return true;
}
static bool apply_control(struct ca *c, const char *command) {
	char verb[32], arg[64] = {0}, extra;
	int count = sscanf(command, "%31s %63s %c", verb, arg, &extra);
	if (count < 1 || count > 2 || !c->configured) return false;
	if (!strcmp(verb, "pause")) c->paused = !c->paused;
	else if (!strcmp(verb, "step")) { c->paused = true; c->manual = true; sim_step(c); }
	else if (!strcmp(verb, "loop")) { c->manual = !c->manual; c->rule_start_ms = now_ms(); }
	else if (!strcmp(verb, "cosmos") || !strcmp(verb, "phase") || !strcmp(verb, "zoom") || !strcmp(verb, "speed")) return cosmos_control(c, verb, arg);
	else if (!strcmp(verb, "edit")) { if (c->cs.on) return false; c->edit = !c->edit; c->paused = c->edit; c->manual = true; if (c->volume && c->edit && c->slice < 0) c->slice = c->depth / 2; update_input(c); }
	else if (!strcmp(verb, "clear")) { memset(c->grid, 0, sim_count(c)); memset(c->age, 0, sim_count(c)); c->manual = true; c->paused = true; }
	else if (!strcmp(verb, "2d") || !strcmp(verb, "3d")) { if (c->cs.on && !strcmp(verb, "3d")) return false; c->volume = !strcmp(verb, "3d"); c->slice = -1; c->edit = false; c->paused = false; c->manual = false; update_input(c); if (!sim_alloc(c)) { c->running = false; return false; } reseed(c); }
	else if (!strcmp(verb, "rotate")) { if (!c->volume) return false; c->slice = -1; c->yaw += !strcmp(arg, "left") ? -.20 : .20; }
	else if (!strcmp(verb, "slice")) {
		if (!c->volume) return false;
		if (!strcmp(arg, "all")) c->slice = -1;
		else if (!strcmp(arg, "next")) c->slice = (c->slice + 1) % c->depth;
		else { char *end; long z = strtol(arg, &end, 10); if (!*arg || *end || z < 0 || z >= c->depth) return false; c->slice = (int)z; }
	} else if (!strcmp(verb, "seed")) {
		if (*arg) { char *end; unsigned long seed = strtoul(arg, &end, 10); if (*end || seed == 0 || seed > UINT32_MAX) return false; c->seed = (unsigned)seed; }
		reseed(c);
	} else if (!strcmp(verb, "rule")) {
		unsigned r; for (r = 0; r < NRULES; r++) if (!strcmp(arg, RULES[r].name)) break;
		if (r == NRULES || c->volume) return false;
		c->rule = r; if (!sim_alloc(c)) { c->running = false; return false; } reseed(c); c->manual = true;
	} else if (!strcmp(verb, "glider") || !strcmp(verb, "blinker")) {
		if (c->volume) return false;
		int x = c->cols / 2, y = c->rows / 2;
		if (!strcmp(verb, "glider")) stamp_glider(c, x, y, 0);
		else for (int dx = -1; dx <= 1; dx++) c->grid[y * c->cols + (x + dx + c->cols) % c->cols] = 1;
		c->manual = true; c->paused = true;
	} else if (!strcmp(verb, "save")) return seed_save(c, *arg ? arg : "default");
	else if (!strcmp(verb, "replay")) return seed_replay(c, *arg ? arg : "default");
	else return false;
	return true;
}
static void receive_control(struct ca *c) {
	char command[256], response[256]; ssize_t n;
	// Bound work per wakeup so a sender cannot starve Wayland dispatch.
	for (int i = 0; i < 32; i++) {
		struct sockaddr_un sender = {0}; socklen_t size = sizeof(sender);
		n = recvfrom(c->control_fd, command, sizeof(command) - 1, 0, (struct sockaddr *)&sender, &size);
		if (n <= 0) break;
		command[n] = 0;
		bool ok = apply_control(c, command);
		if (ok && c->cs.on) snprintf(response, sizeof(response), "OK cosmos %s z=%.2f / %s / %s / rule %s / generation %llu", COS_NAMES[(int)lround(c->cs.zoom) < COS_PHASES ? (int)lround(c->cs.zoom) : COS_PHASES - 1], c->cs.zoom, c->cs.auto_zoom ? "auto" : "hold", c->paused ? "paused" : "running", RULES[c->rule].name, (unsigned long long)c->tick);
		else if (ok) snprintf(response, sizeof(response), "OK %s / %s / generation %llu / %s / slice %d", c->volume ? "3D B5/S4,5" : RULES[c->rule].name, c->manual ? "manual" : "loop", (unsigned long long)c->tick, c->paused ? "paused" : "running", c->slice);
		else snprintf(response, sizeof(response), "ERROR: invalid control, incompatible mode, or seed could not be saved/replayed");
		if (size > sizeof(sa_family_t)) sendto(c->control_fd, response, strlen(response), 0, (struct sockaddr *)&sender, size);
	}
	arm_timer(c);
}
static int control_menu(void) {
	puts("WHITE CUBE FIELD\nCommands: 2d | 3d | pause | step | edit | clear | loop\n"
		"  cosmos on|off|toggle            Cosmic zoom: cells > geometry > land > planet > stars > galaxy > universe\n"
		"  phase cells|geometry|land|planet|stars|galaxy|universe|0..6   Glide to a phase and hold\n"
		"  zoom auto|hold|0..6             Resume the automatic journey, freeze, or set the exact zoom\n"
		"  speed 0.1..8                    Pace of the automatic journey\n"
		"  glider | blinker                 Plant a 2D pattern at the center\n"
		"  rule life|highlife|diamoeba|daynight|anneal|seeds\n"
		"  seed [number]                   Randomize, optionally deterministically\n"
		"  rotate left|right               Rotate the real 18x18x18 volume\n"
		"  slice 0..17|next|all             View or paint a volume layer\n"
		"  save [name] | replay [name]      Save exact cells, rule and random state\n"
		"  quit                            Close controls\n\n"
		"After 'edit', Alt+V exposes the field: drag left to paint, right to erase.\n"
		"Scroll changes 3D layers. Alt+V returns here; 'edit' again runs the field.\n"
		"Painted/saved worlds stay on their rule. 'loop' toggles automatic reseeding.\n"
		"Fullscreen applications or temperatures >=75 C limit the field to 2 ticks/s.\n");
	char line[256]; while (fputs("cube> ", stdout), fflush(stdout), fgets(line, sizeof(line), stdin)) {
		line[strcspn(line, "\r\n")] = 0; if (!strcmp(line, "quit")) break;
		if (*line) send_control(line);
	}
	return 0;
}
static int field_self_test(void) {
	struct ca c = {.W=480, .H=480, .seed=23, .volume=true, .slice=-1, .configured=true};
	if (!sim_alloc(&c)) return 1;
	// Five neighbours in different z layers, including wraparound, must birth origin.
	int w = c.cols, h = c.rows;
	int points[5][3] = {{-1,-1,-1},{1,1,1},{0,0,-1},{0,0,1},{1,0,0}};
	for (int i = 0; i < 5; i++) { int x=(points[i][0]+w)%w, y=(points[i][1]+h)%h, z=(points[i][2]+c.depth)%c.depth; c.grid[(z*h+y)*w+x]=1; }
	sim_step(&c); if (!c.grid[0]) return 2;
	memset(c.grid, 0, sim_count(&c)); c.grid[0]=1; sim_step(&c); if (c.grid[0]) return 3;
	if (!apply_control(&c, "2d") || !apply_control(&c, "clear") || !paint_cell(&c, 25, 25, 1) || !c.grid[c.cols + 1]) return 4;
	if (!apply_control(&c, "3d") || !apply_control(&c, "slice 8") || apply_control(&c, "slice 18")) return 5;
	// Seed roundtrip and deterministic evolution through the persistence format.
	char scratch[] = "/tmp/cube-ca-test-XXXXXX"; if (!mkdtemp(scratch)) return 6;
	const char *old = getenv("XDG_STATE_HOME"); char *oldcopy = old ? strdup(old) : NULL;
	setenv("XDG_STATE_HOME", scratch, 1);
	reseed(&c); size_t count = sim_count(&c); uint8_t *expected = malloc(count);
	bool ok = expected && seed_save(&c, "roundtrip") && !seed_save(&c, "../escape");
	for (int i=0; i<7; i++) sim_step(&c);
	if (expected) memcpy(expected, c.grid, count);
	ok = ok && seed_replay(&c, "roundtrip");
	for (int i=0; i<7; i++) sim_step(&c);
	ok = ok && !memcmp(expected, c.grid, count);
	char path[1200]; if (seed_path(path,sizeof(path),"roundtrip")) unlink(path);
	snprintf(path,sizeof(path),"%s/whitecube-ca",scratch); rmdir(path); rmdir(scratch);
	if (oldcopy) { setenv("XDG_STATE_HOME",oldcopy,1); free(oldcopy); } else unsetenv("XDG_STATE_HOME");
	free(expected); free(c.grid); free(c.next); free(c.age);
	return ok ? 0 : 7;
}
