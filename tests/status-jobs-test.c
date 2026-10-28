/* Real disposable child processes; no desktop, privilege or hardware changes. */
#define _GNU_SOURCE
#undef NDEBUG
#include <sys/types.h>
#include <signal.h>
static int supervisor_kill(pid_t pid, int signal_number);
static char fixture_helper[4096];
#define HELPER fixture_helper
#define kill supervisor_kill
#define main status_app_main
#include "cube-status.c"
#undef main
#undef kill
#include <assert.h>

static bool intercept_kills;
static pid_t killed_pids[4];
static int killed_count;
static int supervisor_kill(pid_t pid, int signal_number) {
    if (!intercept_kills) return kill(pid, signal_number);
    assert(signal_number == SIGKILL && killed_count < 4);
    killed_pids[killed_count++] = pid;
    return 0;
}

static void test_timeout_after_leader_reaped(struct st *s) {
    for (int reaped = 0; reaped <= 1; reaped++) {
        int inherited[2]; assert(!pipe2(inherited, O_CLOEXEC | O_NONBLOCK));
        jobs[0] = (struct job){.active = true, .reaped = reaped, .pid = 424242,
            .fd = inherited[0], .err = -1, .kind = JOB_ACTION, .deadline = monotonic_s() - 1};
        killed_count = 0; intercept_kills = true;
        jobs_pump(s);
        intercept_kills = false;
        assert(killed_count == (reaped ? 0 : 2));
        if (!reaped) assert(killed_pids[0] == -424242 && killed_pids[1] == 424242);
        assert(jobs[0].fd == -1 && jobs[0].err == -1);
        jobs[0].active = false;
        close(inherited[1]);
    }
}

static bool active_jobs(void) {
    for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active) return true;
    return false;
}
static void finish_jobs(struct st *s) {
    double until = monotonic_s() + 4;
    while (active_jobs() && monotonic_s() < until) { jobs_pump(s); usleep(1000); }
    assert(!active_jobs());
    jobs_pump(s);
}
static void clear_notice(struct st *s) { s->notice_text[0] = 0; }
static void run_command(struct st *s, const char *script) {
    const char *argv[] = {"/bin/sh", "-c", script, NULL};
    assert(job_start(JOB_ACTION, argv)); finish_jobs(s);
}

static void test_keyboard(struct st *s) {
    assert(setlocale(LC_CTYPE, "C.UTF-8"));
    s->xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS); assert(s->xkb);
    struct xkb_rule_names names = {.layout = "us", .options = "compose:ralt"};
    s->keymap = xkb_keymap_new_from_names(s->xkb, &names, XKB_KEYMAP_COMPILE_NO_FLAGS); assert(s->keymap);
    s->xkb_state = xkb_state_new(s->keymap); s->compose = wc_compose_create(s->xkb); assert(s->compose);
    s->scale = 1; s->menu_open = true; s->menu.w = 1000; s->menu.h = 700;
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "RALT") - 8);
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "AC11") - 8);
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "AD03") - 8);
    assert(!strcmp(s->query, "é"));
    s->repeat_fd = timerfd_create(CLOCK_MONOTONIC, TFD_CLOEXEC | TFD_NONBLOCK); assert(s->repeat_fd >= 0);
    s->repeat_rate = 25; s->repeat_delay = 1;
    uint32_t backspace = xkb_keymap_key_by_name(s->keymap, "BKSP") - 8;
    k_key(s, NULL, 0, 0, backspace, WL_KEYBOARD_KEY_STATE_PRESSED);
    assert(!s->query[0] && s->repeat_key == backspace);
    struct itimerspec timer; assert(!timerfd_gettime(s->repeat_fd, &timer));
    assert(timer.it_value.tv_sec || timer.it_value.tv_nsec);
    k_key(s, NULL, 0, 0, backspace, WL_KEYBOARD_KEY_STATE_RELEASED);
    assert(!s->repeat_key);
    k_key(s, NULL, 0, 0, backspace, WL_KEYBOARD_KEY_STATE_PRESSED);
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "RALT") - 8);
    k_leave(s, NULL, 0, NULL);
    assert(!s->repeat_key && xkb_compose_state_get_status(s->compose) == XKB_COMPOSE_NOTHING);
    assert(!timerfd_gettime(s->repeat_fd, &timer) && !timer.it_value.tv_sec && !timer.it_value.tv_nsec);
    k_key(s, NULL, 0, 0, backspace, WL_KEYBOARD_KEY_STATE_PRESSED);
    menu_close(s); assert(!s->repeat_key);
    close(s->repeat_fd); s->repeat_fd = -1;
    xkb_state_unref(s->xkb_state); xkb_keymap_unref(s->keymap);
    names.variant = "intl";
    s->keymap = xkb_keymap_new_from_names(s->xkb, &names, XKB_KEYMAP_COMPILE_NO_FLAGS); assert(s->keymap);
    s->xkb_state = xkb_state_new(s->keymap); s->menu_open = true;
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "AC11") - 8);
    handle_status_key(s, xkb_keymap_key_by_name(s->keymap, "AD03") - 8);
    assert(!strcmp(s->query, "é"));
    menu_close(s);
    xkb_state_unref(s->xkb_state); xkb_keymap_unref(s->keymap);
    xkb_compose_state_unref(s->compose); xkb_context_unref(s->xkb);
    s->xkb_state = NULL; s->keymap = NULL; s->compose = NULL; s->xkb = NULL;
}

int main(void) {
    struct st s = {.repeat_fd = -1}; g_st = &s;
    signal(SIGCHLD, SIG_DFL);
    run_command(&s, "printf 'denied by policy\\n' >&2; exit 3");
    assert(strstr(s.notice_text, "denied by policy"));
    clear_notice(&s);
    const char *missing[] = {"/missing-whitecube-test-program", NULL};
    launch_tracked(missing, NULL, "Fixture application"); finish_jobs(&s);
    assert(strstr(s.notice_text, "Fixture application failed") && strstr(s.notice_text, "No such file"));
    clear_notice(&s);
    const char *valid[] = {"/bin/true", NULL};
    launch_tracked(valid, "/missing-whitecube-working-directory", "Fixture folder"); finish_jobs(&s);
    assert(strstr(s.notice_text, "Application folder"));
    clear_notice(&s);
    launch_tracked(valid, NULL, "Successful application"); finish_jobs(&s);
    assert(!s.notice_text[0]);
    // A successfully executed application may later return any exit code.
    const char *later_exit[] = {"/bin/sh", "-c", "exit 17", NULL};
    launch_tracked(later_exit, NULL, "Later application exit"); finish_jobs(&s);
    assert(!s.notice_text[0]);
    const char *sleeping[] = {"/bin/sh", "-c", "sleep 10", NULL};
    assert(job_start(JOB_ACTION, sleeping));
    for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active) jobs[i].deadline = monotonic_s() - 1;
    finish_jobs(&s); assert(strstr(s.notice_text, "timed out"));
    clear_notice(&s);
    for (int i = 0; i < MAX_JOBS; i++) assert(job_start(JOB_ACTION, sleeping));
    assert(!job_start(JOB_ACTION, valid)); assert(strstr(s.notice_text, "busy"));
    for (int i = 0; i < MAX_JOBS; i++) if (jobs[i].active) jobs[i].deadline = monotonic_s() - 1;
    finish_jobs(&s);

    // Read floods remain bounded and retain the diagnostic tail.
    clear_notice(&s);
    const char *flood[] = {"/usr/bin/python3", "-c", "import sys; sys.stdout.write('x'*100000); sys.stderr.write('y'*10000+' final diagnostic'); sys.exit(2)", NULL};
    assert(job_start(JOB_ACTION, flood)); finish_jobs(&s);
    assert(strstr(jobs[0].ebuf, "final diagnostic"));
    assert(strstr(s.notice_text, "final diagnostic"));
    assert(jobs[0].len < sizeof(jobs[0].buf) && jobs[0].elen < sizeof(jobs[0].ebuf));

    char temporary[] = "/tmp/whitecube-status-jobs-XXXXXX";
    char *root = mkdtemp(temporary); assert(root);
    snprintf(fixture_helper, sizeof(fixture_helper), "%s/helper", root);
    char records[4096]; snprintf(records, sizeof(records), "%s/values", root);
    FILE *f = fopen(fixture_helper, "w"); assert(f);
    fprintf(f, "#!/bin/sh\nif [ \"$1\" = volume ]; then printf '%%s\\n' \"$2\" >> '%s'; sleep 0.05; else printf 'sound\\tvolume\\t90\\n'; fi\n", records);
    fclose(f); assert(!chmod(fixture_helper, 0700));
    helper(JOB_SET_SOUND, false, "volume", "10");
    const char *brief[] = {"/bin/sh", "-c", "sleep .2", NULL};
    for (int i = 1; i < MAX_JOBS; i++) assert(job_start(JOB_ACTION, brief));
    for (int i = 11; i <= 90; i++) { char value[16]; snprintf(value, sizeof(value), "%d", i); helper(JOB_SET_SOUND, false, "volume", value); }
    finish_jobs(&s);
    f = fopen(records, "r"); assert(f); char values[64] = {0}; assert(fread(values, 1, sizeof(values) - 1, f) > 0); fclose(f);
    assert(!strcmp(values, "10\n90\n"));
    assert(s.vol_ok && s.vol == 90);
    unlink(fixture_helper); unlink(records); rmdir(root);
    test_timeout_after_leader_reaped(&s);
    test_keyboard(&s);
    int status; assert(waitpid(-1, &status, WNOHANG) == -1 && errno == ECHILD);
    puts("status jobs: denied/hung helpers, bounded output, saturation, slider coalescing, exec failures and child reaping passed");
    return 0;
}
