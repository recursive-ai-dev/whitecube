package sysctl

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
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

	// Pre-create managed file and 10 fake old backups.
	writeFixtureFile(t, managedPath, "# Placeholder\n")
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
