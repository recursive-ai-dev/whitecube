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
