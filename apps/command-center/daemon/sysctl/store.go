package sysctl

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
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
	if err := os.Rename(tmpPath, s.ManagedPath); err != nil {
		os.Remove(tmpPath)
		return err
	}
	return nil
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
