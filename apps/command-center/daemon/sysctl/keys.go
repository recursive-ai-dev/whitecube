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
