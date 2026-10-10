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
	// backupManagedFile only snapshots a *pre-existing* managed file, so
	// (mirroring store_test.go's TestListBackupsAndRestoreBackup) two
	// SetManaged calls are needed to produce one backup: the second call
	// backs up what the first one wrote.
	if _, dbusErr := h.SetManaged([]managedEntryArg{{Key: "kernel.dmesg_restrict", Value: "1"}}, dbus.Sender(":1.1")); dbusErr != nil {
		t.Fatalf("first seed SetManaged: %v", dbusErr)
	}
	if _, dbusErr := h.SetManaged([]managedEntryArg{{Key: "kernel.dmesg_restrict", Value: "0"}}, dbus.Sender(":1.1")); dbusErr != nil {
		t.Fatalf("second seed SetManaged: %v", dbusErr)
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
