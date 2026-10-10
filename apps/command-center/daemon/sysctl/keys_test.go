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
