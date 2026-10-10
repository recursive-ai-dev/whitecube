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
