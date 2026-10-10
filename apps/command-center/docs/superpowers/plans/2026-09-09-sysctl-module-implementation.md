# Sysctl Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the app's simulated command execution with one real, root-level subsystem — sysctl — built on a reusable privileged-execution foundation (Go D-Bus daemon + polkit + a local relay), and ship a new "Sysctl" panel in the existing frontend.

**Architecture:** Browser frontend → local Node/Express **relay** (unprivileged, `127.0.0.1` only) → System-Bus D-Bus call → **`command-center-helperd`** (Go, root, polkit-gated on every mutating call). The daemon is the only privileged piece; the relay only translates HTTP to D-Bus and back.

**Tech Stack:** Go (`github.com/godbus/dbus/v5`) for the daemon; Node/Express + `dbus-next` for the relay; existing React/Vite/TypeScript frontend.

**Spec:** `docs/superpowers/specs/2026-09-09-sysctl-module-design.md`

## Global Constraints

- Managed sysctl file: `/etc/sysctl.d/98-command-center.conf` (the `98-`
  prefix is load-bearing — it must sort, and therefore apply, *before*
  `/etc/sysctl.d/99-airgap-hardening.conf` so that script's values always
  win on any overlapping key).
- D-Bus bus name: `ca.smddev.CommandCenter1`. Object path:
  `/ca/smddev/CommandCenter1/Sysctl`. Interface:
  `ca.smddev.CommandCenter1.Sysctl`.
- Polkit action id for all mutating calls: `ca.smddev.commandcenter.sysctl.apply`.
- Hard-coded guards in the daemon (independent of file order): reject any
  `SetManaged`/`RestoreBackup` batch that would set
  `net.ipv4.ip_forward` above `0`, or `vm.mmap_min_addr` below `4096`.
- Max backups retained: 10, oldest pruned first.
- No byte-level output streaming — every D-Bus method is synchronous
  request/response; the daemon returns full captured output in one call.
- The relay is never called or referred to as a "bridge" (collides with
  real Linux network bridge devices) — it is the **relay**.
- Relay listens on `127.0.0.1:4890` only.
- Reuse the existing UI shell (`Header`, `Sidebar`, `Terminal`,
  `ConfirmationModal`) — do not rebuild the existing "scripts"/"shortcuts"
  concept, which is unrelated to this module.
- No new automated test framework in the existing frontend project (it
  currently has none — `TESTING.md` is a manual QA checklist). Frontend
  verification for this module is `npx tsc --noEmit` plus a new
  `TESTING.md` section, not a new test runner dependency.
- Go daemon tests use only the standard library `testing` package —
  no third-party test framework.
- Relay tests use Node's built-in `node:test` — no third-party test
  framework dependency added just for tests.

---

### Task 1: Sysctl key/path translation and value validation

**Files:**
- Create: `daemon/go.mod`
- Create: `daemon/sysctl/keys.go`
- Test: `daemon/sysctl/keys_test.go`

**Interfaces:**
- Produces: `KeyToPath(procSysPath, key string) (string, error)`,
  `PathToKey(procSysPath, path string) (key string, ok bool)`,
  `IsValidValue(value string) bool` — all used by every later daemon task.

- [ ] **Step 1: Initialize the Go module**

```bash
mkdir -p daemon/sysctl
cd daemon && go mod init command-center-helperd && cd ..
```

- [ ] **Step 2: Write the failing tests**

```go
// daemon/sysctl/keys_test.go
package sysctl

import "testing"

func TestKeyToPath(t *testing.T) {
	cases := []struct {
		key     string
		want    string
		wantErr bool
	}{
		{key: "net.ipv4.ip_forward", want: "/proc/sys/net/ipv4/ip_forward"},
		{key: "kernel.dmesg_restrict", want: "/proc/sys/kernel/dmesg_restrict"},
		{key: "", wantErr: true},
		{key: "net..ipv4", wantErr: true},
		{key: "net.ipv4..", wantErr: true},
		{key: "..", wantErr: true},
	}
	for _, c := range cases {
		got, err := KeyToPath("/proc/sys", c.key)
		if c.wantErr {
			if err == nil {
				t.Errorf("KeyToPath(%q): expected error, got %q", c.key, got)
			}
			continue
		}
		if err != nil {
			t.Errorf("KeyToPath(%q): unexpected error: %v", c.key, err)
			continue
		}
		if got != c.want {
			t.Errorf("KeyToPath(%q) = %q, want %q", c.key, got, c.want)
		}
	}
}

func TestPathToKey(t *testing.T) {
	key, ok := PathToKey("/proc/sys", "/proc/sys/net/ipv4/ip_forward")
	if !ok || key != "net.ipv4.ip_forward" {
		t.Errorf("PathToKey = (%q, %v), want (net.ipv4.ip_forward, true)", key, ok)
	}

	_, ok = PathToKey("/proc/sys", "/etc/passwd")
	if ok {
		t.Error("PathToKey should reject a path outside procSysPath")
	}
}

func TestIsValidValue(t *testing.T) {
	valid := []string{"0", "1", "65536", "|/bin/false"}
	for _, v := range valid {
		if !IsValidValue(v) {
			t.Errorf("IsValidValue(%q) = false, want true", v)
		}
	}
	invalid := []string{"", "line1\nline2", "has\x00null"}
	for _, v := range invalid {
		if IsValidValue(v) {
			t.Errorf("IsValidValue(%q) = true, want false", v)
		}
	}
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd daemon && go test ./sysctl/... -run . -v`
Expected: FAIL — `KeyToPath`, `PathToKey`, `IsValidValue` undefined.

- [ ] **Step 4: Implement**

```go
// daemon/sysctl/keys.go
package sysctl

import (
	"fmt"
	"path/filepath"
	"strings"
)

// KeyToPath translates a dotted sysctl key (e.g. "net.ipv4.ip_forward")
// into its /proc/sys file path. It rejects keys with empty or ".."
// segments to prevent path traversal, and rejects any result that would
// escape procSysPath.
func KeyToPath(procSysPath, key string) (string, error) {
	if key == "" {
		return "", fmt.Errorf("sysctl: empty key")
	}
	segments := strings.Split(key, ".")
	for _, seg := range segments {
		if seg == "" || seg == "." || seg == ".." {
			return "", fmt.Errorf("sysctl: invalid key segment in %q", key)
		}
	}
	joined := filepath.Join(append([]string{procSysPath}, segments...)...)
	cleanBase := filepath.Clean(procSysPath)
	if joined != cleanBase && !strings.HasPrefix(joined, cleanBase+string(filepath.Separator)) {
		return "", fmt.Errorf("sysctl: key %q escapes proc/sys root", key)
	}
	return joined, nil
}

// PathToKey is the inverse of KeyToPath: given a file under procSysPath,
// it returns the dotted key name. ok is false if path is not under
// procSysPath.
func PathToKey(procSysPath, path string) (key string, ok bool) {
	rel, err := filepath.Rel(procSysPath, path)
	if err != nil || strings.HasPrefix(rel, "..") {
		return "", false
	}
	return strings.ReplaceAll(rel, string(filepath.Separator), "."), true
}

// IsValidValue reports whether a value is safe to write into a sysctl.d
// managed file: non-empty, no newlines/NUL bytes, and a reasonable
// maximum length.
func IsValidValue(value string) bool {
	if len(value) == 0 || len(value) > 512 {
		return false
	}
	for _, r := range value {
		if r == '\n' || r == '\r' || r == 0 {
			return false
		}
	}
	return true
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add daemon/go.mod daemon/sysctl/keys.go daemon/sysctl/keys_test.go
git commit -m "Add sysctl key/path translation and value validation"
```

---

### Task 2: Store scaffold and runtime value reads

**Files:**
- Create: `daemon/sysctl/store.go`
- Test: `daemon/sysctl/store_test.go`

**Interfaces:**
- Consumes: `PathToKey` (Task 1).
- Produces: `type Entry struct`, `type ManagedEntry struct`, `NewStore(procSysPath string, sysctlDPaths []string, managedPath string) *Store`, `(s *Store) listRuntimeValues() ([]Entry, error)`.

- [ ] **Step 1: Write the failing test**

```go
// daemon/sysctl/store_test.go
package sysctl

import (
	"os"
	"path/filepath"
	"testing"
)

func writeFixtureFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatal(err)
	}
}

func TestListRuntimeValues(t *testing.T) {
	procSys := t.TempDir()
	writeFixtureFile(t, filepath.Join(procSys, "net/ipv4/ip_forward"), "0\n")
	writeFixtureFile(t, filepath.Join(procSys, "kernel/dmesg_restrict"), "1\n")

	store := NewStore(procSys, nil, filepath.Join(t.TempDir(), "98-command-center.conf"))
	entries, err := store.listRuntimeValues()
	if err != nil {
		t.Fatalf("listRuntimeValues: %v", err)
	}
	if len(entries) != 2 {
		t.Fatalf("expected 2 entries, got %d: %+v", len(entries), entries)
	}
	// Sorted by key: kernel.dmesg_restrict before net.ipv4.ip_forward.
	if entries[0].Key != "kernel.dmesg_restrict" || entries[0].Value != "1" {
		t.Errorf("entries[0] = %+v", entries[0])
	}
	if entries[1].Key != "net.ipv4.ip_forward" || entries[1].Value != "0" {
		t.Errorf("entries[1] = %+v", entries[1])
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd daemon && go test ./sysctl/... -run TestListRuntimeValues -v`
Expected: FAIL — `Store`/`NewStore`/`listRuntimeValues` undefined.

- [ ] **Step 3: Implement**

```go
// daemon/sysctl/store.go
package sysctl

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// Entry describes one sysctl key as returned by Store.List.
type Entry struct {
	Key        string
	Value      string
	SourceFile string // empty if no sysctl.d file sets this key
	IsManaged  bool   // true if SourceFile is this store's managed file
}

// ManagedEntry is the structured input to Store.SetManaged, and the
// structured form a Backup's entries are stored/replayed as.
type ManagedEntry struct {
	Key   string
	Value string
}

// Store reads and writes sysctl state. All paths are configurable so
// tests can point it at a fixture tree instead of the real system.
type Store struct {
	ProcSysPath     string
	SysctlDPaths    []string // highest-priority directory first, e.g. {"/etc/sysctl.d", "/run/sysctl.d", "/usr/lib/sysctl.d"}
	ManagedPath     string
	MaxBackups      int
	RunSysctlSystem func() (string, error)

	mu sync.Mutex
}

func NewStore(procSysPath string, sysctlDPaths []string, managedPath string) *Store {
	return &Store{
		ProcSysPath:  procSysPath,
		SysctlDPaths: sysctlDPaths,
		ManagedPath:  managedPath,
		MaxBackups:   10,
	}
}

// listRuntimeValues walks ProcSysPath and returns every key with its
// current value, sorted by key. Unreadable or non-regular files (some
// /proc/sys entries are write-only, directories, or binary) are skipped.
func (s *Store) listRuntimeValues() ([]Entry, error) {
	var entries []Entry
	err := filepath.WalkDir(s.ProcSysPath, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}
		info, err := d.Info()
		if err != nil || !info.Mode().IsRegular() {
			return nil
		}
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		key, ok := PathToKey(s.ProcSysPath, path)
		if !ok {
			return nil
		}
		entries = append(entries, Entry{
			Key:   key,
			Value: strings.TrimRight(string(raw), "\n"),
		})
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Key < entries[j].Key })
	return entries, nil
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add daemon/sysctl/store.go daemon/sysctl/store_test.go
git commit -m "Add sysctl Store scaffold and runtime value reads"
```

---

### Task 3: sysctl.d source-file precedence and full List()

**Files:**
- Modify: `daemon/sysctl/store.go`
- Test: `daemon/sysctl/store_test.go`

**Interfaces:**
- Consumes: `Store`, `ManagedEntry`, `listRuntimeValues` (Task 2).
- Produces: `(s *Store) List() ([]Entry, error)` — the full read path every later task and the D-Bus handler use.

- [ ] **Step 1: Write the failing test**

```go
// append to daemon/sysctl/store_test.go
func TestListResolvesSourceFilePrecedence(t *testing.T) {
	procSys := t.TempDir()
	writeFixtureFile(t, filepath.Join(procSys, "net/ipv4/ip_forward"), "0\n")
	writeFixtureFile(t, filepath.Join(procSys, "kernel/dmesg_restrict"), "1\n")

	etcDir := t.TempDir()
	runDir := t.TempDir()
	managedPath := filepath.Join(etcDir, "98-command-center.conf")

	// /etc wins over /run for the same filename ("50-example.conf").
	writeFixtureFile(t, filepath.Join(etcDir, "50-example.conf"), "net.ipv4.ip_forward = 0\n")
	writeFixtureFile(t, filepath.Join(runDir, "50-example.conf"), "net.ipv4.ip_forward = 1\n")
	// A lexically later filename in the same directory overrides an earlier one.
	writeFixtureFile(t, filepath.Join(etcDir, "99-airgap-hardening.conf"), "kernel.dmesg_restrict = 1\n")
	writeFixtureFile(t, filepath.Join(etcDir, "98-command-center.conf"), "kernel.dmesg_restrict = 0\n")

	store := NewStore(procSys, []string{etcDir, runDir}, managedPath)
	entries, err := store.List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}

	byKey := map[string]Entry{}
	for _, e := range entries {
		byKey[e.Key] = e
	}

	if got := byKey["net.ipv4.ip_forward"]; got.SourceFile != filepath.Join(etcDir, "50-example.conf") {
		t.Errorf("ip_forward source = %q, want the /etc copy of 50-example.conf", got.SourceFile)
	}
	if got := byKey["kernel.dmesg_restrict"]; got.SourceFile != filepath.Join(etcDir, "99-airgap-hardening.conf") || got.IsManaged {
		t.Errorf("dmesg_restrict = %+v, want sourced from 99-airgap-hardening.conf, not managed", got)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd daemon && go test ./sysctl/... -run TestListResolvesSourceFilePrecedence -v`
Expected: FAIL — `List` undefined (or returns entries with no `SourceFile` populated).

- [ ] **Step 3: Implement**

```go
// append to daemon/sysctl/store.go — add these imports to the existing import block:
// "bufio"

// resolveSourceFiles scans the configured sysctl.d directories in
// precedence order (SysctlDPaths[0] is highest priority, e.g. /etc over
// /run over /usr/lib) and returns, for each key, the path of the last
// file whose assignment applies — matching systemd-sysctl's own
// cross-directory-then-lexical-filename precedence rules.
func (s *Store) resolveSourceFiles() (map[string]string, error) {
	type fileRef struct {
		name string
		path string
	}

	byName := map[string]fileRef{}
	for _, dir := range s.SysctlDPaths {
		matches, err := filepath.Glob(filepath.Join(dir, "*.conf"))
		if err != nil {
			return nil, err
		}
		for _, m := range matches {
			name := filepath.Base(m)
			if _, exists := byName[name]; !exists {
				byName[name] = fileRef{name: name, path: m}
			}
		}
	}

	var refs []fileRef
	for _, ref := range byName {
		refs = append(refs, ref)
	}
	sort.Slice(refs, func(i, j int) bool { return refs[i].name < refs[j].name })

	source := map[string]string{}
	for _, ref := range refs {
		entries, err := parseSysctlFile(ref.path)
		if err != nil {
			return nil, err
		}
		for _, e := range entries {
			source[e.Key] = ref.path
		}
	}
	return source, nil
}

// parseSysctlFile reads one sysctl.d file into key/value entries,
// skipping comments and blank lines and stripping the optional
// "-key = value" ignore-failure prefix.
func parseSysctlFile(path string) ([]ManagedEntry, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var entries []ManagedEntry
	scanner := bufio.NewScanner(f)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") || strings.HasPrefix(line, ";") {
			continue
		}
		line = strings.TrimPrefix(line, "-")
		parts := strings.SplitN(line, "=", 2)
		if len(parts) != 2 {
			continue
		}
		entries = append(entries, ManagedEntry{
			Key:   strings.TrimSpace(parts[0]),
			Value: strings.TrimSpace(parts[1]),
		})
	}
	if err := scanner.Err(); err != nil {
		return nil, err
	}
	return entries, nil
}

// List returns every key under ProcSysPath with its current runtime
// value and, if a sysctl.d file sets it, which file — resolved with
// full cross-directory and lexical-filename precedence.
func (s *Store) List() ([]Entry, error) {
	runtime, err := s.listRuntimeValues()
	if err != nil {
		return nil, err
	}
	sources, err := s.resolveSourceFiles()
	if err != nil {
		return nil, err
	}
	managedClean := filepath.Clean(s.ManagedPath)
	for i := range runtime {
		if src, ok := sources[runtime[i].Key]; ok {
			runtime[i].SourceFile = src
			runtime[i].IsManaged = filepath.Clean(src) == managedClean
		}
	}
	return runtime, nil
}
```

Add `"bufio"` to the top of `daemon/sysctl/store.go`'s import block.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add daemon/sysctl/store.go daemon/sysctl/store_test.go
git commit -m "Resolve sysctl.d source-file precedence in Store.List"
```

---

### Task 4: SetManaged — guards, validation, atomic write, backup, reload

**Files:**
- Modify: `daemon/sysctl/store.go`
- Test: `daemon/sysctl/store_test.go`

**Interfaces:**
- Consumes: `KeyToPath`, `IsValidValue` (Task 1), `Store`, `ManagedEntry`, `parseSysctlFile` (Tasks 2-3).
- Produces: `(s *Store) SetManaged(entries []ManagedEntry) (string, error)`, `(s *Store) listBackupFiles() ([]backupFileRef, error)` (used by Task 5).

- [ ] **Step 1: Write the failing tests**

```go
// append to daemon/sysctl/store_test.go
func newTestStoreForApply(t *testing.T) (*Store, string) {
	t.Helper()
	procSys := t.TempDir()
	writeFixtureFile(t, filepath.Join(procSys, "net/ipv4/ip_forward"), "0\n")
	writeFixtureFile(t, filepath.Join(procSys, "kernel/dmesg_restrict"), "0\n")
	writeFixtureFile(t, filepath.Join(procSys, "vm/mmap_min_addr"), "65536\n")

	etcDir := t.TempDir()
	managedPath := filepath.Join(etcDir, "98-command-center.conf")
	store := NewStore(procSys, []string{etcDir}, managedPath)
	store.RunSysctlSystem = func() (string, error) { return "reloaded ok", nil }
	return store, managedPath
}

func TestSetManagedWritesAndReloads(t *testing.T) {
	store, managedPath := newTestStoreForApply(t)

	output, err := store.SetManaged([]ManagedEntry{{Key: "kernel.dmesg_restrict", Value: "1"}})
	if err != nil {
		t.Fatalf("SetManaged: %v", err)
	}
	if output != "reloaded ok" {
		t.Errorf("output = %q, want %q", output, "reloaded ok")
	}

	data, err := os.ReadFile(managedPath)
	if err != nil {
		t.Fatalf("reading managed file: %v", err)
	}
	if !strings.Contains(string(data), "kernel.dmesg_restrict = 1\n") {
		t.Errorf("managed file = %q, missing expected line", data)
	}
}

func TestSetManagedRejectsUnknownKey(t *testing.T) {
	store, managedPath := newTestStoreForApply(t)
	_, err := store.SetManaged([]ManagedEntry{{Key: "nonexistent.key", Value: "1"}})
	if err == nil {
		t.Fatal("expected an error for an unknown key")
	}
	if _, statErr := os.Stat(managedPath); !os.IsNotExist(statErr) {
		t.Error("managed file should not have been created on validation failure")
	}
}

func TestSetManagedRejectsInvalidValue(t *testing.T) {
	store, _ := newTestStoreForApply(t)
	_, err := store.SetManaged([]ManagedEntry{{Key: "kernel.dmesg_restrict", Value: "bad\nvalue"}})
	if err == nil {
		t.Fatal("expected an error for a value containing a newline")
	}
}

func TestSetManagedRejectsForwardingGuard(t *testing.T) {
	store, _ := newTestStoreForApply(t)
	_, err := store.SetManaged([]ManagedEntry{{Key: "net.ipv4.ip_forward", Value: "1"}})
	if err == nil {
		t.Fatal("expected the airgap invariant guard to reject ip_forward=1")
	}
}

func TestSetManagedRejectsMmapMinAddrGuard(t *testing.T) {
	store, _ := newTestStoreForApply(t)
	_, err := store.SetManaged([]ManagedEntry{{Key: "vm.mmap_min_addr", Value: "0"}})
	if err == nil {
		t.Fatal("expected the airgap invariant guard to reject mmap_min_addr below 4096")
	}
}

func TestSetManagedBacksUpAndPrunesOldBackups(t *testing.T) {
	store, managedPath := newTestStoreForApply(t)

	// Pre-create 10 fake old backups.
	for i := 0; i < 10; i++ {
		writeFixtureFile(t, fmt.Sprintf("%s.bak.%d", managedPath, 1000+i), "kernel.dmesg_restrict = 0\n")
	}

	if _, err := store.SetManaged([]ManagedEntry{{Key: "kernel.dmesg_restrict", Value: "1"}}); err != nil {
		t.Fatalf("SetManaged: %v", err)
	}

	backups, err := store.listBackupFiles()
	if err != nil {
		t.Fatalf("listBackupFiles: %v", err)
	}
	if len(backups) != 10 {
		t.Fatalf("expected 10 backups retained after pruning, got %d", len(backups))
	}
	for _, b := range backups {
		if b.timestamp == 1000 {
			t.Error("oldest backup (timestamp 1000) should have been pruned")
		}
	}
}
```

Add `"fmt"` to the test file's imports if not already present (it is needed for `fmt.Sprintf`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd daemon && go test ./sysctl/... -run TestSetManaged -v`
Expected: FAIL — `SetManaged`/`listBackupFiles` undefined.

- [ ] **Step 3: Implement**

```go
// append to daemon/sysctl/store.go — add "fmt", "strconv", "time" to the import block.

const managedFileHeader = "# Managed by command-center-helperd — do not edit by hand.\n" +
	"# Changes made here will be overwritten on the next Apply.\n"

// guardedInvariants are checked independently of file-order precedence:
// even if this file's load order were ever changed, SetManaged still
// refuses to weaken these two invariants that harden-sysctl.sh also
// hard-codes (net.ipv4.ip_forward must stay 0; vm.mmap_min_addr must
// not drop below a safe floor).
func checkGuardedInvariants(entries []ManagedEntry) error {
	for _, e := range entries {
		switch e.Key {
		case "net.ipv4.ip_forward":
			if e.Value != "0" {
				return fmt.Errorf("sysctl: refusing to set net.ipv4.ip_forward to %q (airgap invariant: must stay 0)", e.Value)
			}
		case "vm.mmap_min_addr":
			n, err := strconv.Atoi(e.Value)
			if err != nil || n < 4096 {
				return fmt.Errorf("sysctl: refusing to set vm.mmap_min_addr to %q (must be a number >= 4096)", e.Value)
			}
		}
	}
	return nil
}

// SetManaged validates entries, backs up the current managed file,
// atomically writes the new one, prunes old backups beyond MaxBackups,
// and reloads sysctl. It holds mu for the duration so concurrent Apply
// calls cannot interleave writes.
func (s *Store) SetManaged(entries []ManagedEntry) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	if err := checkGuardedInvariants(entries); err != nil {
		return "", err
	}

	for _, e := range entries {
		path, err := KeyToPath(s.ProcSysPath, e.Key)
		if err != nil {
			return "", fmt.Errorf("sysctl: invalid key %q: %w", e.Key, err)
		}
		if _, err := os.Stat(path); err != nil {
			return "", fmt.Errorf("sysctl: unknown key %q", e.Key)
		}
		if !IsValidValue(e.Value) {
			return "", fmt.Errorf("sysctl: invalid value for key %q", e.Key)
		}
	}

	if err := s.backupManagedFile(); err != nil {
		return "", fmt.Errorf("sysctl: backup failed: %w", err)
	}
	if err := s.writeManagedFile(entries); err != nil {
		return "", fmt.Errorf("sysctl: write failed: %w", err)
	}
	if err := s.pruneBackups(); err != nil {
		return "", fmt.Errorf("sysctl: prune failed: %w", err)
	}
	if s.RunSysctlSystem == nil {
		return "", fmt.Errorf("sysctl: RunSysctlSystem not configured")
	}
	return s.RunSysctlSystem()
}

func (s *Store) backupManagedFile() error {
	if _, err := os.Stat(s.ManagedPath); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return err
	}
	data, err := os.ReadFile(s.ManagedPath)
	if err != nil {
		return err
	}
	backupPath := fmt.Sprintf("%s.bak.%d", s.ManagedPath, time.Now().UnixNano())
	return os.WriteFile(backupPath, data, 0644)
}

func (s *Store) writeManagedFile(entries []ManagedEntry) error {
	var b strings.Builder
	b.WriteString(managedFileHeader)
	for _, e := range entries {
		fmt.Fprintf(&b, "%s = %s\n", e.Key, e.Value)
	}

	dir := filepath.Dir(s.ManagedPath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".command-center-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	if _, err := tmp.WriteString(b.String()); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Chmod(0644); err != nil {
		tmp.Close()
		os.Remove(tmpPath)
		return err
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return os.Rename(tmpPath, s.ManagedPath)
}

type backupFileRef struct {
	timestamp int64
	path      string
}

// listBackupFiles returns backups of ManagedPath, newest first.
func (s *Store) listBackupFiles() ([]backupFileRef, error) {
	dir := filepath.Dir(s.ManagedPath)
	base := filepath.Base(s.ManagedPath)
	prefix := base + ".bak."

	entries, err := os.ReadDir(dir)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}

	var backups []backupFileRef
	for _, e := range entries {
		if e.IsDir() || !strings.HasPrefix(e.Name(), prefix) {
			continue
		}
		tsStr := strings.TrimPrefix(e.Name(), prefix)
		ts, err := strconv.ParseInt(tsStr, 10, 64)
		if err != nil {
			continue
		}
		backups = append(backups, backupFileRef{timestamp: ts, path: filepath.Join(dir, e.Name())})
	}
	sort.Slice(backups, func(i, j int) bool { return backups[i].timestamp > backups[j].timestamp })
	return backups, nil
}

func (s *Store) pruneBackups() error {
	backups, err := s.listBackupFiles()
	if err != nil {
		return err
	}
	if len(backups) <= s.MaxBackups {
		return nil
	}
	for _, b := range backups[s.MaxBackups:] {
		if err := os.Remove(b.path); err != nil {
			return err
		}
	}
	return nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add daemon/sysctl/store.go daemon/sysctl/store_test.go
git commit -m "Add SetManaged with airgap invariant guards, atomic write, and backups"
```

---

### Task 5: ListBackups and RestoreBackup

**Files:**
- Modify: `daemon/sysctl/store.go`
- Test: `daemon/sysctl/store_test.go`

**Interfaces:**
- Consumes: `listBackupFiles`, `SetManaged` (Task 4), `parseSysctlFile` (Task 3).
- Produces: `type Backup struct`, `(s *Store) ListBackups() ([]Backup, error)`, `(s *Store) RestoreBackup(timestamp int64) (string, error)`.

- [ ] **Step 1: Write the failing test**

```go
// append to daemon/sysctl/store_test.go
func TestListBackupsAndRestoreBackup(t *testing.T) {
	store, managedPath := newTestStoreForApply(t)

	if _, err := store.SetManaged([]ManagedEntry{{Key: "kernel.dmesg_restrict", Value: "1"}}); err != nil {
		t.Fatalf("first SetManaged: %v", err)
	}
	if _, err := store.SetManaged([]ManagedEntry{{Key: "kernel.dmesg_restrict", Value: "0"}}); err != nil {
		t.Fatalf("second SetManaged: %v", err)
	}

	backups, err := store.ListBackups()
	if err != nil {
		t.Fatalf("ListBackups: %v", err)
	}
	if len(backups) != 1 {
		t.Fatalf("expected 1 backup (of the first version), got %d", len(backups))
	}
	if backups[0].Entries[0].Value != "1" {
		t.Errorf("backup entries = %+v, want value 1 (the pre-second-apply version)", backups[0].Entries)
	}

	output, err := store.RestoreBackup(backups[0].Timestamp)
	if err != nil {
		t.Fatalf("RestoreBackup: %v", err)
	}
	if output != "reloaded ok" {
		t.Errorf("output = %q, want %q", output, "reloaded ok")
	}

	data, err := os.ReadFile(managedPath)
	if err != nil {
		t.Fatalf("reading managed file: %v", err)
	}
	if !strings.Contains(string(data), "kernel.dmesg_restrict = 1\n") {
		t.Errorf("managed file after restore = %q, want dmesg_restrict = 1", data)
	}

	// The restore itself should have created a fresh backup of the pre-restore version.
	backupsAfter, err := store.ListBackups()
	if err != nil {
		t.Fatalf("ListBackups after restore: %v", err)
	}
	if len(backupsAfter) != 2 {
		t.Fatalf("expected 2 backups after restore, got %d", len(backupsAfter))
	}
}

func TestRestoreBackupRejectsUnknownTimestamp(t *testing.T) {
	store, _ := newTestStoreForApply(t)
	_, err := store.RestoreBackup(999999)
	if err == nil {
		t.Fatal("expected an error restoring a nonexistent backup timestamp")
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd daemon && go test ./sysctl/... -run "TestListBackupsAndRestoreBackup|TestRestoreBackupRejectsUnknownTimestamp" -v`
Expected: FAIL — `Backup`/`ListBackups`/`RestoreBackup` undefined.

- [ ] **Step 3: Implement**

```go
// append to daemon/sysctl/store.go

// Backup describes one snapshot of the managed file.
type Backup struct {
	Timestamp int64
	Entries   []ManagedEntry
}

// ListBackups returns available backups of the managed file, newest first.
func (s *Store) ListBackups() ([]Backup, error) {
	refs, err := s.listBackupFiles()
	if err != nil {
		return nil, err
	}
	var backups []Backup
	for _, ref := range refs {
		entries, err := parseSysctlFile(ref.path)
		if err != nil {
			return nil, err
		}
		backups = append(backups, Backup{Timestamp: ref.timestamp, Entries: entries})
	}
	return backups, nil
}

// RestoreBackup re-applies a prior backup's entries through SetManaged,
// so a restore gets the same validation, guards, locking, and fresh
// backup-of-the-restore as any other apply — never a silent bypass of
// the normal safety flow.
func (s *Store) RestoreBackup(timestamp int64) (string, error) {
	refs, err := s.listBackupFiles()
	if err != nil {
		return "", err
	}
	for _, ref := range refs {
		if ref.timestamp == timestamp {
			entries, err := parseSysctlFile(ref.path)
			if err != nil {
				return "", err
			}
			return s.SetManaged(entries)
		}
	}
	return "", fmt.Errorf("sysctl: no backup with timestamp %d", timestamp)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Commit**

```bash
git add daemon/sysctl/store.go daemon/sysctl/store_test.go
git commit -m "Add ListBackups and RestoreBackup"
```

---

### Task 6: Polkit authorization client

**Files:**
- Create: `daemon/polkit/polkit.go`
- Test: `daemon/polkit/polkit_test.go`

**Interfaces:**
- Produces: `func NewChecker(conn *dbus.Conn) *Checker`, `(c *Checker) CheckAuthorization(sender dbus.Sender, actionID string) error` — consumed by Task 7's `Handler`, which references this method via a small local interface (so it can be faked in tests without a real bus).

- [ ] **Step 1: Add the D-Bus dependency**

```bash
cd daemon && go get github.com/godbus/dbus/v5@v5.1.0
```

- [ ] **Step 2: Write the failing test**

Real `CheckAuthorization` calls require a live system bus and a running
polkit daemon, which aren't available in a unit test — that round trip is
exercised by Task 7's handler tests (with a fake `authorizer`) and by the
manual smoke test (Task 16). This test covers the one pure piece: the
not-authorized error message.

```go
// daemon/polkit/polkit_test.go
package polkit

import "testing"

func TestErrNotAuthorizedMessage(t *testing.T) {
	err := errNotAuthorized{actionID: "ca.smddev.commandcenter.sysctl.apply"}
	want := "polkit: not authorized for action ca.smddev.commandcenter.sysctl.apply"
	if err.Error() != want {
		t.Errorf("Error() = %q, want %q", err.Error(), want)
	}
}
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd daemon && go test ./polkit/... -v`
Expected: FAIL — `errNotAuthorized` undefined.

- [ ] **Step 4: Implement**

```go
// daemon/polkit/polkit.go
package polkit

import (
	"github.com/godbus/dbus/v5"
)

const (
	busName    = "org.freedesktop.PolicyKit1"
	objectPath = "/org/freedesktop/PolicyKit1/Authority"
	ifaceName  = "org.freedesktop.PolicyKit1.Authority"

	// allowUserInteraction lets PolicyKit prompt the caller's session
	// authentication agent (the native GNOME/KDE password dialog) if
	// the action requires it.
	allowUserInteraction uint32 = 1
)

// Checker asks PolicyKit whether a D-Bus caller is authorized for an
// action, prompting the caller's session polkit agent if needed.
type Checker struct {
	conn *dbus.Conn
}

func NewChecker(conn *dbus.Conn) *Checker {
	return &Checker{conn: conn}
}

// subject is PolicyKit's (sa{sv}) "Subject" struct for a system-bus caller.
type subject struct {
	Kind    string
	Details map[string]dbus.Variant
}

type errNotAuthorized struct{ actionID string }

func (e errNotAuthorized) Error() string {
	return "polkit: not authorized for action " + e.actionID
}

// CheckAuthorization returns nil if the caller identified by
// callerBusName is authorized for actionID (interactively, via the
// caller's session polkit agent, if the action's policy requires it).
func (c *Checker) CheckAuthorization(callerBusName dbus.Sender, actionID string) error {
	obj := c.conn.Object(busName, dbus.ObjectPath(objectPath))

	subj := subject{
		Kind: "system-bus-name",
		Details: map[string]dbus.Variant{
			"name": dbus.MakeVariant(string(callerBusName)),
		},
	}

	var isAuthorized, isChallenge bool
	var details map[string]string

	call := obj.Call(ifaceName+".CheckAuthorization", 0,
		subj, actionID, map[string]string{}, allowUserInteraction, "")
	if call.Err != nil {
		return call.Err
	}
	if err := call.Store(&isAuthorized, &isChallenge, &details); err != nil {
		return err
	}
	if !isAuthorized {
		return errNotAuthorized{actionID: actionID}
	}
	return nil
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd daemon && go test ./polkit/... -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add daemon/go.mod daemon/go.sum daemon/polkit/polkit.go daemon/polkit/polkit_test.go
git commit -m "Add polkit CheckAuthorization client"
```

---

### Task 7: D-Bus handler and daemon entrypoint

**Files:**
- Create: `daemon/sysctl/dbus.go`
- Test: `daemon/sysctl/dbus_test.go`
- Create: `daemon/cmd/command-center-helperd/main.go`

**Interfaces:**
- Consumes: `Store` (Tasks 2-5), `polkit.Checker` (Task 6, via a local `authorizer` interface for testability).
- Produces: `BusName`, `ObjectPath`, `InterfaceName`, `ApplyActionID` constants (used by the relay's real D-Bus client in Task 10 and by the install files in Task 8), `NewHandler(store *Store, auth authorizer) *Handler`.

- [ ] **Step 1: Write the failing tests**

These call the `Handler`'s Go methods directly (not over a real D-Bus
transport) against a fixture `Store` and a fake `authorizer` — this
exercises all of the real logic (auth gating plus every store operation)
without standing up a private D-Bus session bus in CI. The wire-level
D-Bus export is verified by the manual smoke test (Task 16).

```go
// daemon/sysctl/dbus_test.go
package sysctl

import (
	"errors"
	"os"
	"testing"

	"github.com/godbus/dbus/v5"
)

type fakeAuthorizer struct {
	allow bool
}

func (f fakeAuthorizer) CheckAuthorization(sender dbus.Sender, actionID string) error {
	if f.allow {
		return nil
	}
	return errors.New("not authorized")
}

func newTestHandler(t *testing.T, allow bool) (*Handler, string) {
	t.Helper()
	store, managedPath := newTestStoreForApply(t)
	return NewHandler(store, fakeAuthorizer{allow: allow}), managedPath
}

func TestHandlerListReturnsEntries(t *testing.T) {
	h, _ := newTestHandler(t, true)
	rows, dbusErr := h.List()
	if dbusErr != nil {
		t.Fatalf("List: %v", dbusErr)
	}
	if len(rows) == 0 {
		t.Fatal("expected at least one row from the fixture proc/sys tree")
	}
}

func TestHandlerSetManagedDeniesUnauthorized(t *testing.T) {
	h, managedPath := newTestHandler(t, false)
	_, dbusErr := h.SetManaged([]managedEntryArg{{Key: "kernel.dmesg_restrict", Value: "1"}}, dbus.Sender(":1.1"))
	if dbusErr == nil {
		t.Fatal("expected an authorization error")
	}
	if _, statErr := os.Stat(managedPath); statErr == nil {
		t.Error("managed file should not exist after a denied SetManaged")
	}
}

func TestHandlerSetManagedAppliesWhenAuthorized(t *testing.T) {
	h, _ := newTestHandler(t, true)
	output, dbusErr := h.SetManaged([]managedEntryArg{{Key: "kernel.dmesg_restrict", Value: "1"}}, dbus.Sender(":1.1"))
	if dbusErr != nil {
		t.Fatalf("SetManaged: %v", dbusErr)
	}
	if output != "reloaded ok" {
		t.Errorf("output = %q, want %q", output, "reloaded ok")
	}
}

func TestHandlerListBackupsAndRestoreBackup(t *testing.T) {
	h, _ := newTestHandler(t, true)
	if _, dbusErr := h.SetManaged([]managedEntryArg{{Key: "kernel.dmesg_restrict", Value: "1"}}, dbus.Sender(":1.1")); dbusErr != nil {
		t.Fatalf("seed SetManaged: %v", dbusErr)
	}
	backups, dbusErr := h.ListBackups()
	if dbusErr != nil {
		t.Fatalf("ListBackups: %v", dbusErr)
	}
	if len(backups) != 1 {
		t.Fatalf("expected 1 backup, got %d", len(backups))
	}

	_, dbusErr = h.RestoreBackup(backups[0].Timestamp, dbus.Sender(":1.1"))
	if dbusErr != nil {
		t.Fatalf("RestoreBackup: %v", dbusErr)
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd daemon && go test ./sysctl/... -run TestHandler -v`
Expected: FAIL — `Handler`/`NewHandler`/`managedEntryArg` undefined.

- [ ] **Step 3: Implement the handler**

```go
// daemon/sysctl/dbus.go
package sysctl

import (
	"github.com/godbus/dbus/v5"
)

const (
	BusName       = "ca.smddev.CommandCenter1"
	ObjectPath    = "/ca/smddev/CommandCenter1/Sysctl"
	InterfaceName = "ca.smddev.CommandCenter1.Sysctl"
	ApplyActionID = "ca.smddev.commandcenter.sysctl.apply"
)

// authorizer is satisfied by *polkit.Checker; defined locally so tests
// can supply a fake without needing a real D-Bus/polkit round trip.
type authorizer interface {
	CheckAuthorization(sender dbus.Sender, actionID string) error
}

// Handler exports the Sysctl D-Bus interface, gating every mutating
// call on polkit authorization of the caller.
type Handler struct {
	store *Store
	auth  authorizer
}

func NewHandler(store *Store, auth authorizer) *Handler {
	return &Handler{store: store, auth: auth}
}

// listResult mirrors Entry as a D-Bus (sssb) struct.
type listResult struct {
	Key        string
	Value      string
	SourceFile string
	IsManaged  bool
}

func (h *Handler) List() ([]listResult, *dbus.Error) {
	entries, err := h.store.List()
	if err != nil {
		return nil, dbus.MakeFailedError(err)
	}
	out := make([]listResult, len(entries))
	for i, e := range entries {
		out[i] = listResult{e.Key, e.Value, e.SourceFile, e.IsManaged}
	}
	return out, nil
}

// managedEntryArg mirrors ManagedEntry as a D-Bus (ss) struct, used for
// both SetManaged's input and each backup's entries in ListBackups.
type managedEntryArg struct {
	Key   string
	Value string
}

func (h *Handler) SetManaged(entries []managedEntryArg, sender dbus.Sender) (string, *dbus.Error) {
	if err := h.auth.CheckAuthorization(sender, ApplyActionID); err != nil {
		return "", dbus.MakeFailedError(err)
	}
	converted := make([]ManagedEntry, len(entries))
	for i, e := range entries {
		converted[i] = ManagedEntry{Key: e.Key, Value: e.Value}
	}
	output, err := h.store.SetManaged(converted)
	if err != nil {
		return "", dbus.MakeFailedError(err)
	}
	return output, nil
}

type backupResult struct {
	Timestamp int64
	Entries   []managedEntryArg
}

func (h *Handler) ListBackups() ([]backupResult, *dbus.Error) {
	backups, err := h.store.ListBackups()
	if err != nil {
		return nil, dbus.MakeFailedError(err)
	}
	out := make([]backupResult, len(backups))
	for i, b := range backups {
		entries := make([]managedEntryArg, len(b.Entries))
		for j, e := range b.Entries {
			entries[j] = managedEntryArg{Key: e.Key, Value: e.Value}
		}
		out[i] = backupResult{Timestamp: b.Timestamp, Entries: entries}
	}
	return out, nil
}

func (h *Handler) RestoreBackup(timestamp int64, sender dbus.Sender) (string, *dbus.Error) {
	if err := h.auth.CheckAuthorization(sender, ApplyActionID); err != nil {
		return "", dbus.MakeFailedError(err)
	}
	output, err := h.store.RestoreBackup(timestamp)
	if err != nil {
		return "", dbus.MakeFailedError(err)
	}
	return output, nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd daemon && go test ./sysctl/... -v`
Expected: PASS (all tests so far)

- [ ] **Step 5: Write the daemon entrypoint**

This wires the real system bus, the real polkit checker, and the real
`sysctl --system` call together. It has no automated test — it is pure
process wiring with no branching logic — so its verification is `go
build` succeeding here, and the manual smoke test (Task 16) actually
running it.

```go
// daemon/cmd/command-center-helperd/main.go
package main

import (
	"fmt"
	"log"
	"os"
	"os/exec"

	"github.com/godbus/dbus/v5"
	"github.com/godbus/dbus/v5/introspect"

	"command-center-helperd/polkit"
	"command-center-helperd/sysctl"
)

const managedPath = "/etc/sysctl.d/98-command-center.conf"

func realRunSysctlSystem() (string, error) {
	cmd := exec.Command("sysctl", "--system")
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func main() {
	conn, err := dbus.ConnectSystemBus()
	if err != nil {
		log.Fatalf("connect to system bus: %v", err)
	}
	defer conn.Close()

	reply, err := conn.RequestName(sysctl.BusName, dbus.NameFlagDoNotQueue)
	if err != nil {
		log.Fatalf("request name: %v", err)
	}
	if reply != dbus.RequestNameReplyPrimaryOwner {
		log.Fatalf("bus name %s already owned", sysctl.BusName)
	}

	store := sysctl.NewStore(
		"/proc/sys",
		[]string{"/etc/sysctl.d", "/run/sysctl.d", "/usr/lib/sysctl.d"},
		managedPath,
	)
	store.RunSysctlSystem = realRunSysctlSystem

	auth := polkit.NewChecker(conn)
	handler := sysctl.NewHandler(store, auth)

	if err := conn.Export(handler, sysctl.ObjectPath, sysctl.InterfaceName); err != nil {
		log.Fatalf("export handler: %v", err)
	}

	node := &introspect.Node{
		Name: sysctl.ObjectPath,
		Interfaces: []introspect.Interface{
			introspect.IntrospectData,
			{
				Name:    sysctl.InterfaceName,
				Methods: introspect.Methods(handler),
			},
		},
	}
	if err := conn.Export(introspect.NewIntrospectable(node), sysctl.ObjectPath, "org.freedesktop.DBus.Introspectable"); err != nil {
		log.Fatalf("export introspection: %v", err)
	}

	fmt.Fprintf(os.Stderr, "command-center-helperd: listening on %s\n", sysctl.BusName)
	select {}
}
```

- [ ] **Step 6: Verify the daemon builds**

Run: `cd daemon && go build ./...`
Expected: builds with no errors (this does not run the daemon — it has
no effect on your real system; it only checks compilation).

- [ ] **Step 7: Commit**

```bash
git add daemon/sysctl/dbus.go daemon/sysctl/dbus_test.go daemon/cmd
git commit -m "Add sysctl D-Bus handler and daemon entrypoint"
```

---

### Task 8: Install artifacts (systemd unit, D-Bus policy, polkit policy, installer script)

**Files:**
- Create: `install/command-center-helperd.service`
- Create: `install/ca.smddev.CommandCenter1.service`
- Create: `install/ca.smddev.CommandCenter1.conf.in`
- Create: `install/ca.smddev.commandcenter.sysctl.policy`
- Create: `scripts/install-daemon.sh`

**Interfaces:**
- Consumes: `sysctl.BusName`, `sysctl.ObjectPath`, `sysctl.ApplyActionID` constant values (Task 7) — these files hard-code the same literal strings, so if those constants ever change, these five files must change with them.

- [ ] **Step 1: systemd unit (D-Bus-activated, not a standing always-on service)**

```ini
# install/command-center-helperd.service
[Unit]
Description=Command Center privileged helper daemon

[Service]
Type=dbus
BusName=ca.smddev.CommandCenter1
ExecStart=/usr/local/libexec/command-center/command-center-helperd
```

- [ ] **Step 2: D-Bus service-activation file**

```ini
# install/ca.smddev.CommandCenter1.service
[D-BUS Service]
Name=ca.smddev.CommandCenter1
Exec=/usr/local/libexec/command-center/command-center-helperd
User=root
SystemdService=command-center-helperd.service
```

- [ ] **Step 3: D-Bus system-bus policy template**

The `__COMMAND_CENTER_USER__` placeholder is substituted by the installer
with the invoking (non-root) user, restricting who may even send messages
to the daemon — defense in depth beneath the per-action polkit check.

```xml
<!-- install/ca.smddev.CommandCenter1.conf.in -->
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <policy user="root">
    <allow own="ca.smddev.CommandCenter1"/>
  </policy>
  <policy user="__COMMAND_CENTER_USER__">
    <allow send_destination="ca.smddev.CommandCenter1"/>
    <allow receive_sender="ca.smddev.CommandCenter1"/>
  </policy>
  <policy context="default">
    <deny send_destination="ca.smddev.CommandCenter1"/>
  </policy>
</busconfig>
```

- [ ] **Step 4: Polkit action policy**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- install/ca.smddev.commandcenter.sysctl.policy -->
<!DOCTYPE policyconfig PUBLIC
 "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Command Center</vendor>
  <action id="ca.smddev.commandcenter.sysctl.apply">
    <description>Apply sysctl configuration changes</description>
    <message>Authentication is required to change system kernel parameters</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
  </action>
</policyconfig>
```

- [ ] **Step 5: Installer script**

```bash
#!/usr/bin/env bash
# scripts/install-daemon.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "error: must be run with sudo/root" >&2
  exit 1
fi

TARGET_USER="${SUDO_USER:-}"
if [[ -z "$TARGET_USER" ]]; then
  echo "error: run this via 'sudo scripts/install-daemon.sh' from a normal user session (SUDO_USER must be set)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="$SCRIPT_DIR/install"
BIN_DIR="/usr/local/libexec/command-center"

echo "==> Building daemon binary"
( cd "$SCRIPT_DIR/daemon" && go build -o "$SCRIPT_DIR/daemon/command-center-helperd" ./cmd/command-center-helperd )

echo "==> Installing binary to $BIN_DIR"
install -d -m 0755 "$BIN_DIR"
install -m 0755 "$SCRIPT_DIR/daemon/command-center-helperd" "$BIN_DIR/command-center-helperd"

echo "==> Installing systemd unit"
install -m 0644 "$INSTALL_DIR/command-center-helperd.service" /etc/systemd/system/command-center-helperd.service

echo "==> Installing D-Bus activation file"
install -m 0644 "$INSTALL_DIR/ca.smddev.CommandCenter1.service" /usr/share/dbus-1/system-services/ca.smddev.CommandCenter1.service

echo "==> Installing D-Bus policy for user $TARGET_USER"
sed "s/__COMMAND_CENTER_USER__/$TARGET_USER/" "$INSTALL_DIR/ca.smddev.CommandCenter1.conf.in" > /usr/share/dbus-1/system.d/ca.smddev.CommandCenter1.conf
chmod 0644 /usr/share/dbus-1/system.d/ca.smddev.CommandCenter1.conf

echo "==> Installing polkit action"
install -m 0644 "$INSTALL_DIR/ca.smddev.commandcenter.sysctl.policy" /usr/share/polkit-1/actions/ca.smddev.commandcenter.sysctl.policy

echo "==> Reloading D-Bus and systemd"
systemctl reload dbus.service || true
systemctl daemon-reload

echo "==> Done. The daemon will be D-Bus-activated on first use by $TARGET_USER."
```

```bash
chmod +x scripts/install-daemon.sh
```

- [ ] **Step 6: Verify without installing**

Run: `bash -n scripts/install-daemon.sh` (syntax check only — does not
run the script or touch your real system)
Expected: no output, exit code 0

Run (if `xmllint` is available; skip otherwise): `xmllint --noout install/ca.smddev.CommandCenter1.conf.in install/ca.smddev.commandcenter.sysctl.policy install/ca.smddev.CommandCenter1.service`
Expected: no output, exit code 0

Do **not** run `sudo scripts/install-daemon.sh` yet — that's Task 16, the
end-to-end smoke test, run deliberately and ideally on a disposable VM
first.

- [ ] **Step 7: Commit**

```bash
git add install scripts/install-daemon.sh
git commit -m "Add daemon install artifacts (systemd, D-Bus policy, polkit, installer)"
```

---

### Task 9: Relay scaffold and HTTP server (tested against a fake D-Bus client)

**Files:**
- Create: `relay/package.json`
- Create: `relay/tsconfig.json`
- Create: `relay/src/dbusClient.ts`
- Create: `relay/src/server.ts`
- Test: `relay/src/server.test.ts`

**Interfaces:**
- Produces: `interface SysctlDbusClient`, `interface SysctlEntry`, `interface SysctlBackup` (also consumed by Task 10 and by the frontend's types in Task 11 — field names/order must match exactly), `createServer(client: SysctlDbusClient): Express`.

- [ ] **Step 1: Scaffold the package**

```json
// relay/package.json
{
  "name": "command-center-relay",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "scripts": {
    "build": "tsc",
    "start": "npm run build && node dist/index.js",
    "test": "npm run build && node --test dist/server.test.js"
  },
  "dependencies": {
    "express": "^4.21.0",
    "dbus-next": "^0.10.2"
  },
  "devDependencies": {
    "@types/express": "^4.17.21",
    "@types/node": "^22.14.0",
    "typescript": "~5.8.2"
  }
}
```

```json
// relay/tsconfig.json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true
  },
  "include": ["src"]
}
```

```bash
cd relay && npm install
```

- [ ] **Step 2: Define the D-Bus client interface**

```typescript
// relay/src/dbusClient.ts
export interface SysctlEntry {
  key: string;
  value: string;
  sourceFile: string;
  isManaged: boolean;
}

export interface SysctlBackup {
  timestamp: number;
  entries: { key: string; value: string }[];
}

export interface SysctlDbusClient {
  list(): Promise<SysctlEntry[]>;
  setManaged(entries: { key: string; value: string }[]): Promise<string>;
  listBackups(): Promise<SysctlBackup[]>;
  restoreBackup(timestamp: number): Promise<string>;
}
```

- [ ] **Step 3: Write the failing tests**

```typescript
// relay/src/server.test.ts
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from './server.js';
import type { SysctlDbusClient, SysctlEntry } from './dbusClient.js';

function fakeClient(overrides: Partial<SysctlDbusClient> = {}): SysctlDbusClient {
  return {
    list: async () => [],
    setManaged: async () => '',
    listBackups: async () => [],
    restoreBackup: async () => '',
    ...overrides,
  };
}

test('GET /api/sysctl returns entries from the dbus client', async () => {
  const entries: SysctlEntry[] = [{ key: 'net.ipv4.tcp_syncookies', value: '1', sourceFile: '', isManaged: false }];
  const app = createServer(fakeClient({ list: async () => entries }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl`);
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.deepEqual(body.entries, entries);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/apply rejects a malformed body', async () => {
  const app = createServer(fakeClient());
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/apply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ entries: [{ key: 'x' }] }),
    });
    assert.equal(res.status, 400);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/apply forwards valid entries and returns output', async () => {
  let received: unknown;
  const app = createServer(fakeClient({
    setManaged: async (entries) => { received = entries; return 'applied ok'; },
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/apply`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ entries: [{ key: 'net.ipv4.tcp_syncookies', value: '1' }] }),
    });
    assert.equal(res.status, 200);
    const body = await res.json();
    assert.equal(body.output, 'applied ok');
    assert.deepEqual(received, [{ key: 'net.ipv4.tcp_syncookies', value: '1' }]);
  } finally {
    server.close();
  }
});

test('GET /api/sysctl/backups returns backups from the dbus client', async () => {
  const app = createServer(fakeClient({
    listBackups: async () => [{ timestamp: 123, entries: [{ key: 'kernel.dmesg_restrict', value: '0' }] }],
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/backups`);
    const body = await res.json();
    assert.equal(body.backups.length, 1);
    assert.equal(body.backups[0].timestamp, 123);
  } finally {
    server.close();
  }
});

test('POST /api/sysctl/backups/:timestamp/restore forwards the timestamp', async () => {
  let received: unknown;
  const app = createServer(fakeClient({
    restoreBackup: async (timestamp) => { received = timestamp; return 'restored ok'; },
  }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl/backups/123/restore`, { method: 'POST' });
    const body = await res.json();
    assert.equal(body.output, 'restored ok');
    assert.equal(received, 123);
  } finally {
    server.close();
  }
});

test('errors from the dbus client surface as 502s', async () => {
  const app = createServer(fakeClient({ list: async () => { throw new Error('daemon unreachable'); } }));
  const server = app.listen(0);
  const { port } = server.address() as { port: number };
  try {
    const res = await fetch(`http://127.0.0.1:${port}/api/sysctl`);
    assert.equal(res.status, 502);
    const body = await res.json();
    assert.equal(body.error, 'daemon unreachable');
  } finally {
    server.close();
  }
});
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd relay && npm test`
Expected: build fails — `./server.js` does not exist yet.

- [ ] **Step 5: Implement the server**

```typescript
// relay/src/server.ts
import express, { Express } from 'express';
import { SysctlDbusClient } from './dbusClient.js';

export function createServer(client: SysctlDbusClient): Express {
  const app = express();
  app.use(express.json());

  app.get('/api/sysctl', async (_req, res) => {
    try {
      const entries = await client.list();
      res.json({ entries });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.post('/api/sysctl/apply', async (req, res) => {
    const entries = req.body?.entries;
    if (!Array.isArray(entries) || entries.some((e) => typeof e?.key !== 'string' || typeof e?.value !== 'string')) {
      res.status(400).json({ error: 'body must be { entries: [{ key, value }] }' });
      return;
    }
    try {
      const output = await client.setManaged(entries);
      res.json({ output });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.get('/api/sysctl/backups', async (_req, res) => {
    try {
      const backups = await client.listBackups();
      res.json({ backups });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.post('/api/sysctl/backups/:timestamp/restore', async (req, res) => {
    const timestamp = Number(req.params.timestamp);
    if (!Number.isFinite(timestamp)) {
      res.status(400).json({ error: 'timestamp must be numeric' });
      return;
    }
    try {
      const output = await client.restoreBackup(timestamp);
      res.json({ output });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  return app;
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd relay && npm test`
Expected: all 6 tests pass

- [ ] **Step 7: Commit**

```bash
git add relay/package.json relay/package-lock.json relay/tsconfig.json relay/src/dbusClient.ts relay/src/server.ts relay/src/server.test.ts
git commit -m "Add relay HTTP server tested against a fake D-Bus client"
```

---

### Task 10: Real D-Bus-backed client and relay entrypoint

**Files:**
- Create: `relay/src/realDbusClient.ts`
- Create: `relay/src/index.ts`

**Interfaces:**
- Consumes: `SysctlDbusClient`, `SysctlEntry`, `SysctlBackup` (Task 9), `createServer` (Task 9), and the daemon's `BusName`/`ObjectPath`/`InterfaceName` constant *values* (Task 7) — hard-coded here as literal strings; if those Go constants ever change, this file must change with them.

There is no automated test for this file: it requires a live daemon on
the real system bus, which isn't available in a unit test. Its
verification is `npx tsc --noEmit` (compiles cleanly) plus the manual
smoke test (Task 16), which is the only thing that proves the wire
format round-trips correctly end to end.

- [ ] **Step 1: Implement the real client**

```typescript
// relay/src/realDbusClient.ts
import { MessageBus, systemBus } from 'dbus-next';
import type { SysctlDbusClient, SysctlEntry, SysctlBackup } from './dbusClient.js';

const BUS_NAME = 'ca.smddev.CommandCenter1';
const OBJECT_PATH = '/ca/smddev/CommandCenter1/Sysctl';
const INTERFACE_NAME = 'ca.smddev.CommandCenter1.Sysctl';

export async function createRealDbusClient(bus: MessageBus = systemBus()): Promise<SysctlDbusClient> {
  const proxy = await bus.getProxyObject(BUS_NAME, OBJECT_PATH);
  const iface = proxy.getInterface(INTERFACE_NAME);

  return {
    async list(): Promise<SysctlEntry[]> {
      const rows: [string, string, string, boolean][] = await iface.List();
      return rows.map(([key, value, sourceFile, isManaged]) => ({ key, value, sourceFile, isManaged }));
    },
    async setManaged(entries): Promise<string> {
      return iface.SetManaged(entries.map((e) => [e.key, e.value]));
    },
    async listBackups(): Promise<SysctlBackup[]> {
      const rows: [number, [string, string][]][] = await iface.ListBackups();
      return rows.map(([timestamp, entries]) => ({
        timestamp,
        entries: entries.map(([key, value]) => ({ key, value })),
      }));
    },
    async restoreBackup(timestamp): Promise<string> {
      return iface.RestoreBackup(timestamp);
    },
  };
}
```

- [ ] **Step 2: Implement the entrypoint**

```typescript
// relay/src/index.ts
import { createServer } from './server.js';
import { createRealDbusClient } from './realDbusClient.js';

const PORT = Number(process.env.PORT ?? 4890);

const client = await createRealDbusClient();
const app = createServer(client);
app.listen(PORT, '127.0.0.1', () => {
  console.log(`command-center-relay listening on http://127.0.0.1:${PORT}`);
});
```

- [ ] **Step 3: Verify it compiles**

Run: `cd relay && npx tsc --noEmit`
Expected: no errors. (This does not start the relay or contact a real
daemon — it only type-checks.)

- [ ] **Step 4: Commit**

```bash
git add relay/src/realDbusClient.ts relay/src/index.ts
git commit -m "Add real D-Bus-backed relay client and entrypoint"
```

---

### Task 11: Frontend types, sysctl catalog, and API client

**Files:**
- Modify: `types.ts`
- Create: `sysctlCatalog.ts`
- Create: `sysctlClient.ts`

**Interfaces:**
- Produces: `SysctlEntry`, `SysctlBackup`, `SysctlRiskTier`, `SysctlCatalogEntry` types (consumed by Tasks 12-14), `SYSCTL_CATALOG` (Task 12), `fetchSysctlEntries`, `applySysctlEntries`, `fetchSysctlBackups`, `restoreSysctlBackup` (Tasks 12-14). Field names/shapes must match `relay/src/dbusClient.ts` (Task 9) exactly, since they cross the same JSON wire.

- [ ] **Step 1: Extend types.ts**

```typescript
// add to types.ts
export interface SysctlEntry {
  key: string;
  value: string;
  sourceFile: string;
  isManaged: boolean;
}

export interface SysctlBackup {
  timestamp: number;
  entries: { key: string; value: string }[];
}

export type SysctlRiskTier = 'info' | 'low' | 'medium' | 'high';

export interface SysctlCatalogEntry {
  description: string;
  risk: SysctlRiskTier;
}
```

Also change the `ViewMode` type:

```typescript
// change in types.ts:
export type ViewMode = 'Dashboard' | 'Nautilus' | 'Shortcuts' | 'Sysctl';
```

- [ ] **Step 2: Add the curated catalog**

```typescript
// sysctlCatalog.ts
import { SysctlCatalogEntry } from './types';

export const SYSCTL_CATALOG: Record<string, SysctlCatalogEntry> = {
  'net.ipv4.ip_forward': {
    description: 'Enables the kernel to route IPv4 packets between interfaces. Forced to 0 on this machine by the airgap hardening suite.',
    risk: 'high',
  },
  'net.ipv4.conf.all.rp_filter': {
    description: 'Reverse-path filtering for spoofed-source packet protection.',
    risk: 'medium',
  },
  'net.ipv4.tcp_syncookies': {
    description: 'SYN cookie protection against SYN-flood connection exhaustion.',
    risk: 'low',
  },
  'net.ipv4.conf.all.accept_redirects': {
    description: 'Whether ICMP redirects can change this host’s routing table.',
    risk: 'medium',
  },
  'net.ipv4.conf.all.accept_source_route': {
    description: 'Whether source-routed packets are accepted.',
    risk: 'medium',
  },
  'kernel.dmesg_restrict': {
    description: 'Restricts dmesg access to privileged (CAP_SYSLOG) users.',
    risk: 'low',
  },
  'kernel.kptr_restrict': {
    description: 'Hides kernel pointers from unprivileged (or, at the strictest setting, all) reads, hardening against info leaks.',
    risk: 'low',
  },
  'kernel.yama.ptrace_scope': {
    description: 'Restricts which processes can ptrace which other processes.',
    risk: 'medium',
  },
  'kernel.sysrq': {
    description: 'Controls which magic SysRq key operations are permitted.',
    risk: 'medium',
  },
  'vm.overcommit_memory': {
    description: 'Controls whether the kernel allows memory overcommit; changes OOM-killer behavior system-wide.',
    risk: 'high',
  },
  'vm.mmap_min_addr': {
    description: 'Lowest address a process may mmap. Guarded by this app: cannot be set below 4096.',
    risk: 'high',
  },
  'vm.swappiness': {
    description: 'How aggressively the kernel swaps memory pages to disk.',
    risk: 'low',
  },
  'fs.protected_hardlinks': {
    description: 'Restricts hardlink creation to files you own, closing a privilege-escalation vector.',
    risk: 'medium',
  },
  'fs.protected_symlinks': {
    description: 'Restricts symlink following in world-writable sticky directories.',
    risk: 'medium',
  },
  'fs.suid_dumpable': {
    description: 'Whether setuid programs can produce core dumps.',
    risk: 'medium',
  },
  'fs.file-max': {
    description: 'System-wide limit on the number of open file handles.',
    risk: 'low',
  },
  'net.core.somaxconn': {
    description: 'Maximum length of the pending-connection queue for listening sockets.',
    risk: 'low',
  },
};
```

- [ ] **Step 3: Add the relay API client**

```typescript
// sysctlClient.ts
import { SysctlEntry, SysctlBackup } from './types';

const RELAY_BASE_URL = 'http://127.0.0.1:4890';

async function parseJsonOrThrow(res: Response): Promise<any> {
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.error || `Request failed with status ${res.status}`);
  }
  return body;
}

export async function fetchSysctlEntries(): Promise<SysctlEntry[]> {
  const res = await fetch(`${RELAY_BASE_URL}/api/sysctl`);
  const body = await parseJsonOrThrow(res);
  return body.entries;
}

export async function applySysctlEntries(entries: { key: string; value: string }[]): Promise<string> {
  const res = await fetch(`${RELAY_BASE_URL}/api/sysctl/apply`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ entries }),
  });
  const body = await parseJsonOrThrow(res);
  return body.output;
}

export async function fetchSysctlBackups(): Promise<SysctlBackup[]> {
  const res = await fetch(`${RELAY_BASE_URL}/api/sysctl/backups`);
  const body = await parseJsonOrThrow(res);
  return body.backups;
}

export async function restoreSysctlBackup(timestamp: number): Promise<string> {
  const res = await fetch(`${RELAY_BASE_URL}/api/sysctl/backups/${timestamp}/restore`, { method: 'POST' });
  const body = await parseJsonOrThrow(res);
  return body.output;
}
```

- [ ] **Step 4: Verify it compiles**

Run: `npx tsc --noEmit`
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add types.ts sysctlCatalog.ts sysctlClient.ts
git commit -m "Add sysctl types, curated catalog, and relay API client"
```

---

### Task 12: SysctlRow and SysctlPanel components

**Files:**
- Create: `components/SysctlRow.tsx`
- Create: `components/SysctlPanel.tsx`

**Interfaces:**
- Consumes: `SysctlEntry`, `SysctlCatalogEntry` (Task 11), `SYSCTL_CATALOG`, `fetchSysctlEntries` (Task 11).
- Produces: `SysctlPanel` props `{ onRequestApply: (pending: { key: string; value: string }[]) => void; refreshKey: number }` — consumed by Task 14's `App.tsx` wiring.

- [ ] **Step 1: SysctlRow**

```tsx
// components/SysctlRow.tsx
import React from 'react';
import { SysctlEntry, SysctlCatalogEntry } from '../types';

interface SysctlRowProps {
  entry: SysctlEntry;
  catalogInfo?: SysctlCatalogEntry;
  pendingValue?: string;
  onChange: (key: string, value: string) => void;
}

const RISK_STYLES: Record<string, string> = {
  info: 'bg-white/10 text-white/60',
  low: 'bg-blue-500/20 text-blue-300',
  medium: 'bg-yellow-500/20 text-yellow-300',
  high: 'bg-red-500/20 text-red-300',
};

const SysctlRow: React.FC<SysctlRowProps> = ({ entry, catalogInfo, pendingValue, onChange }) => {
  const displayValue = pendingValue ?? entry.value;
  const isDirty = pendingValue !== undefined && pendingValue !== entry.value;

  return (
    <div className={`flex items-center gap-4 px-4 py-3 border-b border-white/5 ${isDirty ? 'bg-ubuntu-orange/10' : ''}`}>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <code className="text-sm text-white font-mono truncate">{entry.key}</code>
          {catalogInfo && (
            <span className={`text-[10px] px-2 py-0.5 rounded-full uppercase font-bold ${RISK_STYLES[catalogInfo.risk]}`}>
              {catalogInfo.risk}
            </span>
          )}
        </div>
        {catalogInfo && <p className="text-xs text-white/40 mt-0.5">{catalogInfo.description}</p>}
        <p className="text-[10px] text-white/30 mt-0.5">
          {entry.isManaged
            ? 'Managed by Command Center'
            : entry.sourceFile
            ? `Set by ${entry.sourceFile}`
            : 'Runtime only (no boot-persisted file)'}
        </p>
      </div>
      <input
        type="text"
        value={displayValue}
        onChange={(e) => onChange(entry.key, e.target.value)}
        className={`w-40 bg-black/20 border rounded px-2 py-1 text-sm font-mono text-white outline-none ${
          isDirty ? 'border-ubuntu-orange' : 'border-white/10'
        }`}
      />
      {isDirty && <span className="text-[10px] text-white/30 font-mono">was {entry.value}</span>}
    </div>
  );
};

export default SysctlRow;
```

- [ ] **Step 2: SysctlPanel**

```tsx
// components/SysctlPanel.tsx
import React, { useEffect, useMemo, useState } from 'react';
import { SysctlEntry } from '../types';
import { SYSCTL_CATALOG } from '../sysctlCatalog';
import { fetchSysctlEntries } from '../sysctlClient';
import SysctlRow from './SysctlRow';

interface SysctlPanelProps {
  onRequestApply: (pending: { key: string; value: string }[]) => void;
  refreshKey: number;
}

const SysctlPanel: React.FC<SysctlPanelProps> = ({ onRequestApply, refreshKey }) => {
  const [entries, setEntries] = useState<SysctlEntry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [pending, setPending] = useState<Record<string, string>>({});

  useEffect(() => {
    setPending({});
    fetchSysctlEntries()
      .then((result) => {
        setEntries(result);
        setLoadError(null);
      })
      .catch((err) => setLoadError(err.message));
  }, [refreshKey]);

  const filtered = useMemo(() => {
    if (!searchQuery) return entries;
    const q = searchQuery.toLowerCase();
    return entries.filter((e) => e.key.toLowerCase().includes(q));
  }, [entries, searchQuery]);

  const handleChange = (key: string, value: string) => {
    setPending((prev) => ({ ...prev, [key]: value }));
  };

  const pendingList = Object.entries(pending)
    .filter(([key, value]) => entries.find((e) => e.key === key)?.value !== value)
    .map(([key, value]) => ({ key, value }));

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 border-b border-white/5 flex items-center gap-4">
        <input
          type="text"
          placeholder="Search sysctl keys..."
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          className="flex-1 bg-black/20 border border-white/10 rounded-full px-4 py-2 text-sm text-white outline-none"
        />
        <button
          disabled={pendingList.length === 0}
          onClick={() => onRequestApply(pendingList)}
          className="px-4 py-2 bg-ubuntu-orange rounded text-sm font-bold text-white disabled:opacity-30 disabled:cursor-not-allowed"
        >
          Apply {pendingList.length > 0 ? `(${pendingList.length})` : ''}
        </button>
      </div>

      {loadError && (
        <div className="p-4 text-sm text-red-400">
          Could not reach the relay: {loadError}. Is the daemon installed and running?
        </div>
      )}

      <div className="flex-1 overflow-y-auto">
        {filtered.map((entry) => (
          <SysctlRow
            key={entry.key}
            entry={entry}
            catalogInfo={SYSCTL_CATALOG[entry.key]}
            pendingValue={pending[entry.key]}
            onChange={handleChange}
          />
        ))}
      </div>
    </div>
  );
};

export default SysctlPanel;
```

- [ ] **Step 3: Verify it compiles**

Run: `npx tsc --noEmit`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add components/SysctlRow.tsx components/SysctlPanel.tsx
git commit -m "Add SysctlRow and SysctlPanel components"
```

---

### Task 13: SysctlHistory component

**Files:**
- Create: `components/SysctlHistory.tsx`

**Interfaces:**
- Consumes: `SysctlBackup` (Task 11), `fetchSysctlBackups` (Task 11).
- Produces: `SysctlHistory` props `{ refreshKey: number; onRequestRestore: (timestamp: number, entries: { key: string; value: string }[]) => void }` — consumed by Task 14's `App.tsx` wiring.

- [ ] **Step 1: Implement**

```tsx
// components/SysctlHistory.tsx
import React, { useEffect, useState } from 'react';
import { SysctlBackup } from '../types';
import { fetchSysctlBackups } from '../sysctlClient';

interface SysctlHistoryProps {
  refreshKey: number;
  onRequestRestore: (timestamp: number, entries: { key: string; value: string }[]) => void;
}

const formatBackupTime = (timestampNanos: number): string => {
  return new Date(timestampNanos / 1_000_000).toLocaleString();
};

const SysctlHistory: React.FC<SysctlHistoryProps> = ({ refreshKey, onRequestRestore }) => {
  const [backups, setBackups] = useState<SysctlBackup[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchSysctlBackups()
      .then((result) => {
        setBackups(result);
        setError(null);
      })
      .catch((err) => setError(err.message));
  }, [refreshKey]);

  if (error) {
    return <p className="text-xs text-red-400 p-4">Could not load history: {error}</p>;
  }

  if (backups.length === 0) {
    return <p className="text-xs text-white/30 p-4">No backups yet — apply a change to create one.</p>;
  }

  return (
    <div className="p-4 space-y-2">
      <h3 className="text-xs font-bold text-white/40 uppercase tracking-wider">History</h3>
      {backups.map((backup) => (
        <div key={backup.timestamp} className="flex items-center justify-between bg-black/20 rounded px-3 py-2">
          <div>
            <p className="text-xs text-white/70">{formatBackupTime(backup.timestamp)}</p>
            <p className="text-[10px] text-white/30">{backup.entries.length} key(s)</p>
          </div>
          <button
            onClick={() => onRequestRestore(backup.timestamp, backup.entries)}
            className="text-xs px-3 py-1 rounded bg-white/10 hover:bg-ubuntu-orange text-white transition-colors"
          >
            Restore
          </button>
        </div>
      ))}
    </div>
  );
};

export default SysctlHistory;
```

Note: Go's `time.Now().UnixNano()` (Task 4) produces nanoseconds; dividing
by `1_000_000` here converts to the milliseconds `Date` expects.

- [ ] **Step 2: Verify it compiles**

Run: `npx tsc --noEmit`
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add components/SysctlHistory.tsx
git commit -m "Add SysctlHistory component"
```

---

### Task 14: Wire the Sysctl panel into App.tsx and Sidebar

**Files:**
- Modify: `components/Sidebar.tsx`
- Modify: `App.tsx`

**Interfaces:**
- Consumes: `SysctlPanel` (Task 12), `SysctlHistory` (Task 13), `applySysctlEntries`, `restoreSysctlBackup` (Task 11), the existing `ConfirmationModal` (unmodified).

- [ ] **Step 1: Add a Sidebar entry**

```tsx
// in components/Sidebar.tsx, add to the `tools` array:
const tools = [
  { id: 'Nautilus', icon: 'folder_special', label: 'Nautilus Menus' },
  { id: 'Shortcuts', icon: 'keyboard_command_key', label: 'Shortcut Groups' },
  { id: 'Sysctl', icon: 'settings_suggest', label: 'Sysctl' },
];
```

- [ ] **Step 2: Wire App.tsx**

Add imports:

```tsx
// add to App.tsx's import block
import SysctlPanel from './components/SysctlPanel';
import SysctlHistory from './components/SysctlHistory';
import { applySysctlEntries, restoreSysctlBackup } from './sysctlClient';
```

Add state (alongside the existing modal/confirmation state):

```tsx
// add inside the App component, near the other state declarations
const [sysctlRefreshKey, setSysctlRefreshKey] = useState(0);
const [pendingSysctlApply, setPendingSysctlApply] = useState<{ key: string; value: string }[] | null>(null);
const [pendingSysctlRestore, setPendingSysctlRestore] = useState<{
  timestamp: number;
  entries: { key: string; value: string }[];
} | null>(null);
```

Add handlers (alongside the existing `confirmExecution`):

```tsx
const confirmSysctlApply = async () => {
  if (!pendingSysctlApply) return;
  const entries = pendingSysctlApply;
  setPendingSysctlApply(null);
  addCommandLog(`sysctl apply (${entries.length} key(s))`);
  try {
    const output = await applySysctlEntries(entries);
    addLog(output, 'success');
    setSysctlRefreshKey((k) => k + 1);
  } catch (err) {
    addLog((err as Error).message, 'error');
  }
};

const confirmSysctlRestore = async () => {
  if (!pendingSysctlRestore) return;
  const { timestamp } = pendingSysctlRestore;
  setPendingSysctlRestore(null);
  addCommandLog(`sysctl restore ${timestamp}`);
  try {
    const output = await restoreSysctlBackup(timestamp);
    addLog(output, 'success');
    setSysctlRefreshKey((k) => k + 1);
  } catch (err) {
    addLog((err as Error).message, 'error');
  }
};
```

Add the panel to the main content area, alongside the existing
`Dashboard`/`Nautilus`/`Shortcuts` view blocks:

```tsx
{activeView === 'Sysctl' && (
  <div className="h-full flex">
    <div className="flex-1 min-w-0">
      <SysctlPanel onRequestApply={setPendingSysctlApply} refreshKey={sysctlRefreshKey} />
    </div>
    <div className="w-72 border-l border-white/5 overflow-y-auto shrink-0">
      <SysctlHistory
        refreshKey={sysctlRefreshKey}
        onRequestRestore={(timestamp, entries) => setPendingSysctlRestore({ timestamp, entries })}
      />
    </div>
  </div>
)}
```

Add two more `ConfirmationModal` instances, alongside the existing one
(reusing the component as-is, no changes to `ConfirmationModal.tsx`):

```tsx
<ConfirmationModal
  isOpen={!!pendingSysctlApply}
  title="Apply Sysctl Changes"
  message="This will write /etc/sysctl.d/98-command-center.conf and reload sysctl. You may be prompted to authenticate."
  command={(pendingSysctlApply ?? []).map((e) => `${e.key} = ${e.value}`).join('\n')}
  onCancel={() => setPendingSysctlApply(null)}
  onConfirm={confirmSysctlApply}
/>
<ConfirmationModal
  isOpen={!!pendingSysctlRestore}
  title="Restore Sysctl Backup"
  message="This will restore a previous sysctl configuration and reload sysctl. You may be prompted to authenticate."
  command={(pendingSysctlRestore?.entries ?? []).map((e) => `${e.key} = ${e.value}`).join('\n')}
  onCancel={() => setPendingSysctlRestore(null)}
  onConfirm={confirmSysctlRestore}
/>
```

- [ ] **Step 3: Verify it compiles**

Run: `npx tsc --noEmit`
Expected: no errors

- [ ] **Step 4: Run the dev server and click through it manually**

Run: `npm run dev`

Expected: the app loads, a "Sysctl" entry appears in the Configuration
section of the Sidebar, and clicking it shows the panel with a "Could not
reach the relay" error (since the relay isn't running yet at this point
in the plan) — that error path is itself confirmation the wiring is
correct; full end-to-end behavior is verified in Task 16.

- [ ] **Step 5: Commit**

```bash
git add components/Sidebar.tsx App.tsx
git commit -m "Wire the Sysctl panel into App.tsx and Sidebar"
```

---

### Task 15: Manual test plan additions

**Files:**
- Modify: `TESTING.md`

**Interfaces:**
- None (documentation only).

- [ ] **Step 1: Add a new section following the existing numbered format**

```markdown
## 7) Sysctl Module (Real System Execution)

**Objective:** Verify the Sysctl panel reads and writes real system state
through the relay and daemon, with the airgap coexistence guards intact.

**Prerequisites:**
- The daemon is installed (`sudo scripts/install-daemon.sh`) and the
  relay is running (`cd relay && npm start`).
- Ideally run this on a disposable VM/container the first time, not a
  daily-driver machine.

**Steps:**
1. Open the Sysctl panel. Confirm the values shown for a few keys (e.g.
   `kernel.dmesg_restrict`, `net.ipv4.tcp_syncookies`) match a separate
   terminal's `sysctl <key>` output.
2. Edit a low-risk key (e.g. `net.ipv4.tcp_syncookies`) to a different
   valid value. Confirm the row highlights as pending with an old → new
   diff, and nothing on the real system has changed yet.
3. Click Apply. Confirm the `ConfirmationModal` shows the pending diff,
   and confirm it. Confirm you are prompted by your desktop's polkit
   authentication dialog.
4. After authenticating, confirm the Terminal shows the daemon's real
   `sysctl --system` output, and a separate terminal's `sysctl <key>`
   now shows the new value.
5. Confirm a new entry appears in the History panel. Click Restore on
   it and confirm the value reverts, again via a real polkit prompt.
6. Attempt to set `net.ipv4.ip_forward` to `1`. Confirm the apply is
   rejected (the daemon's airgap invariant guard) and the real value
   stays `0`.
7. Attempt to set `vm.mmap_min_addr` to `0`. Confirm the apply is
   rejected (the mmap_min_addr floor guard).
8. Stop the relay or the daemon and reload the panel. Confirm a clear
   "daemon not installed / unreachable" message appears, rather than a
   generic error.
```

- [ ] **Step 2: Commit**

```bash
git add TESTING.md
git commit -m "Add manual test plan for the Sysctl module"
```

---

### Task 16: Manual end-to-end smoke test (run by a human, on a disposable VM first)

This task has no code changes. It is the final gate before considering
the module done, and it is the only step in this plan that actually
installs the daemon and touches real system state — do not run it on a
primary machine without first trying it on a disposable VM or container.

- [ ] **Step 1: Install the daemon**

```bash
sudo scripts/install-daemon.sh
```

Expected: completes without error; `systemctl status command-center-helperd` (after triggering activation in step 3) shows it running as root.

- [ ] **Step 2: Start the relay**

```bash
cd relay && npm start
```

Expected: logs `command-center-relay listening on http://127.0.0.1:4890`.

- [ ] **Step 3: Start the frontend and run through `TESTING.md` section 7**

```bash
npm run dev
```

Work through every step added in Task 15, in order, on the real machine
(or VM). In particular confirm:
- Values shown in the panel match `sysctl <key>` in a real terminal.
- Applying a change triggers a real polkit authentication dialog (not
  just an in-app confirmation).
- `journalctl -u command-center-helperd` shows an audit entry for the
  apply, with old/new content.
- `net.ipv4.ip_forward` cannot be pushed above `0` and `vm.mmap_min_addr`
  cannot be pushed below `4096`, regardless of what's typed into the
  panel.
- `cat /etc/sysctl.d/98-command-center.conf /etc/sysctl.d/99-airgap-hardening.conf`
  — confirm that for any key set in both files, the *airgap* file's value
  is the one actually in effect (`sysctl <key>`), per the `98-`/`99-`
  load-order design.

- [ ] **Step 2 (record result):** Note the outcome (pass/fail, and any
  fixes needed) in the PR description or commit message for this task —
  there is no code to commit if everything passes, so this step is the
  record of having verified it.
