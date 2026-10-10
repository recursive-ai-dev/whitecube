package polkit

import "testing"

func TestErrNotAuthorizedMessage(t *testing.T) {
	err := errNotAuthorized{actionID: "ca.smddev.commandcenter.sysctl.apply"}
	want := "polkit: not authorized for action ca.smddev.commandcenter.sysctl.apply"
	if err.Error() != want {
		t.Errorf("Error() = %q, want %q", err.Error(), want)
	}
}
