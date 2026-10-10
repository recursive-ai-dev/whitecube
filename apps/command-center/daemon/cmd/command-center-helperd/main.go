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
