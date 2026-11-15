/* SPDX-License-Identifier: MIT
 *
 * introspect.c - org.freedesktop.DBus.Properties, Introspectable and the
 * deny-by-default interface policy.
 *
 * Properties.Get must answer *immediately*: GTK4, libadwaita and Chromium
 * probe org.freedesktop.portal.{Settings,FileChooser}.version before they do
 * anything else, and a missing answer means a 25 second client stall.
 */
#include "whitecube_portal.h"

#include <string.h>

const wc_iface_version wc_iface_versions[] = {
    { "org.freedesktop.portal.Settings",    2 },
    { "org.freedesktop.portal.FileChooser", 3 },
    { "org.freedesktop.portal.OpenURI",     3 },
    { NULL, 0 }
};

/* Invasive or unimplemented portals: answer NotAllowed at once so clients
 * fall back instead of hanging.  Anything not listed here and not implemented
 * gets UnknownInterface/UnknownMethod - also immediate. */
static const char *const denied_ifaces[] = {
    "org.freedesktop.portal.Camera",
    "org.freedesktop.portal.Location",
    "org.freedesktop.portal.Secret",
    "org.freedesktop.portal.ScreenCast",
    "org.freedesktop.portal.RemoteDesktop",
    "org.freedesktop.portal.Screenshot",
    "org.freedesktop.portal.Clipboard",
    "org.freedesktop.portal.InputCapture",
    "org.freedesktop.portal.GlobalShortcuts",
    "org.freedesktop.portal.Background",
    "org.freedesktop.portal.DynamicLauncher",
    "org.freedesktop.portal.Account",
    "org.freedesktop.portal.Email",
    "org.freedesktop.portal.Print",
    "org.freedesktop.portal.Wallpaper",
    "org.freedesktop.portal.Notification",
    "org.freedesktop.portal.Usb",
    "org.freedesktop.portal.Documents",
    "org.freedesktop.portal.FileTransfer",
    "org.freedesktop.portal.GameMode",
    "org.freedesktop.portal.MemoryMonitor",
    "org.freedesktop.portal.NetworkMonitor",
    "org.freedesktop.portal.PowerProfileMonitor",
    "org.freedesktop.portal.ProxyResolver",
    "org.freedesktop.portal.Registry",
    "org.freedesktop.portal.Trash",
    NULL
};

bool wc_iface_denied(const char *iface)
{
    size_t i;

    if (!iface)
        return false;
    for (i = 0; denied_ifaces[i]; i++)
        if (!strcmp(iface, denied_ifaces[i]))
            return true;
    return false;
}

bool wc_iface_version_get(const char *iface, uint32_t *out)
{
    size_t i;

    if (!iface)
        return false;
    for (i = 0; wc_iface_versions[i].iface; i++) {
        if (!strcmp(iface, wc_iface_versions[i].iface)) {
            *out = wc_iface_versions[i].version;
            return true;
        }
    }
    return false;
}

/* ------------------------------------------------------------------ */
/* Properties                                                          */
/* ------------------------------------------------------------------ */

static void prop_get(wc_portal *p, const wc_msg *m)
{
    wc_reader r;
    const char *iface, *prop;
    uint32_t version;
    wc_writer w;

    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    iface = wc_r_string(&r);
    prop = wc_r_string(&r);
    if (!wc_r_ok(&r) || !iface || !prop) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "Get expects (ss)");
        return;
    }
    if (wc_iface_denied(iface)) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED, "%s is not available",
                     iface);
        return;
    }
    if (!wc_iface_version_get(iface, &version)) {
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_IFACE, "no such interface: %s",
                     iface);
        return;
    }
    if (strcmp(prop, "version") != 0) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "no such property: %s",
                     prop);
        return;
    }
    wc_w_init(&w);
    wc_w_variant(&w, "u");
    wc_w_u32(&w, version);
    wc_bus_reply(&p->bus, m, "v", &w);
    wc_w_free(&w);
}

static void prop_get_all(wc_portal *p, const wc_msg *m)
{
    wc_reader r;
    const char *iface;
    uint32_t version;
    wc_writer w;
    wc_array_mark dict;

    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    iface = wc_r_string(&r);
    if (!wc_r_ok(&r) || !iface) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "GetAll expects (s)");
        return;
    }
    if (wc_iface_denied(iface)) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED, "%s is not available",
                     iface);
        return;
    }
    if (!wc_iface_version_get(iface, &version)) {
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_IFACE, "no such interface: %s",
                     iface);
        return;
    }
    wc_w_init(&w);
    dict = wc_w_dict_begin(&w);
    wc_w_dict_entry(&w, "version", "u");
    wc_w_u32(&w, version);
    wc_w_dict_end(&w, dict);
    wc_bus_reply(&p->bus, m, "a{sv}", &w);
    wc_w_free(&w);
}

static void prop_set(wc_portal *p, const wc_msg *m)
{
    wc_reader r;
    const char *iface, *prop;
    wc_value v;
    uint32_t version;

    wc_r_init(&r, m->body, m->body_len, m->big_endian);
    iface = wc_r_string(&r);
    prop = wc_r_string(&r);
    if (!wc_r_ok(&r) || !iface || !prop || !wc_value_read(&r, &v)) {
        wc_bus_error(&p->bus, m, WC_ERR_INVALID_ARGS, "Set expects (ssv)");
        return;
    }
    if (wc_iface_denied(iface)) {
        wc_bus_error(&p->bus, m, WC_ERR_NOT_ALLOWED, "%s is not available",
                     iface);
        return;
    }
    if (!wc_iface_version_get(iface, &version)) {
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_IFACE, "no such interface: %s",
                     iface);
        return;
    }
    wc_bus_error(&p->bus, m, WC_ERR_READONLY,
                 "portal properties are read-only at runtime");
}

void wc_properties_handle(wc_portal *p, const wc_msg *m)
{
    const char *member = m->member ? m->member : "";

    if (!strcmp(member, "Get"))
        prop_get(p, m);
    else if (!strcmp(member, "GetAll"))
        prop_get_all(p, m);
    else if (!strcmp(member, "Set"))
        prop_set(p, m);
    else
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_METH,
                     "org.freedesktop.DBus.Properties has no method '%s'",
                     member);
}

/* ------------------------------------------------------------------ */
/* Introspection                                                       */
/* ------------------------------------------------------------------ */

static const char desktop_xml[] =
"<!DOCTYPE node PUBLIC \"-//freedesktop//DTD D-BUS Object Introspection 1.0//EN\"\n"
" \"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd\">\n"
"<node>\n"
"  <interface name=\"org.freedesktop.DBus.Properties\">\n"
"    <method name=\"Get\">\n"
"      <arg type=\"s\" name=\"interface_name\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"property_name\" direction=\"in\"/>\n"
"      <arg type=\"v\" name=\"value\" direction=\"out\"/>\n"
"    </method>\n"
"    <method name=\"GetAll\">\n"
"      <arg type=\"s\" name=\"interface_name\" direction=\"in\"/>\n"
"      <arg type=\"a{sv}\" name=\"properties\" direction=\"out\"/>\n"
"    </method>\n"
"    <method name=\"Set\">\n"
"      <arg type=\"s\" name=\"interface_name\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"property_name\" direction=\"in\"/>\n"
"      <arg type=\"v\" name=\"value\" direction=\"in\"/>\n"
"    </method>\n"
"    <signal name=\"PropertiesChanged\">\n"
"      <arg type=\"s\" name=\"interface_name\"/>\n"
"      <arg type=\"a{sv}\" name=\"changed_properties\"/>\n"
"      <arg type=\"as\" name=\"invalidated_properties\"/>\n"
"    </signal>\n"
"  </interface>\n"
"  <interface name=\"org.freedesktop.DBus.Introspectable\">\n"
"    <method name=\"Introspect\">\n"
"      <arg type=\"s\" name=\"xml_data\" direction=\"out\"/>\n"
"    </method>\n"
"  </interface>\n"
"  <interface name=\"org.freedesktop.DBus.Peer\">\n"
"    <method name=\"Ping\"/>\n"
"    <method name=\"GetMachineId\">\n"
"      <arg type=\"s\" name=\"machine_uuid\" direction=\"out\"/>\n"
"    </method>\n"
"  </interface>\n"
"  <interface name=\"org.freedesktop.portal.Settings\">\n"
"    <method name=\"Read\">\n"
"      <arg type=\"s\" name=\"namespace\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"key\" direction=\"in\"/>\n"
"      <arg type=\"v\" name=\"value\" direction=\"out\"/>\n"
"    </method>\n"
"    <method name=\"ReadOne\">\n"
"      <arg type=\"s\" name=\"namespace\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"key\" direction=\"in\"/>\n"
"      <arg type=\"v\" name=\"value\" direction=\"out\"/>\n"
"    </method>\n"
"    <method name=\"ReadAll\">\n"
"      <arg type=\"as\" name=\"namespaces\" direction=\"in\"/>\n"
"      <arg type=\"a{sa{sv}}\" name=\"values\" direction=\"out\"/>\n"
"    </method>\n"
"    <signal name=\"SettingChanged\">\n"
"      <arg type=\"s\" name=\"namespace\"/>\n"
"      <arg type=\"s\" name=\"key\"/>\n"
"      <arg type=\"v\" name=\"value\"/>\n"
"    </signal>\n"
"    <property name=\"version\" type=\"u\" access=\"read\"/>\n"
"  </interface>\n"
"  <interface name=\"org.freedesktop.portal.FileChooser\">\n"
"    <method name=\"OpenFile\">\n"
"      <arg type=\"s\" name=\"parent_window\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"title\" direction=\"in\"/>\n"
"      <arg type=\"a{sv}\" name=\"options\" direction=\"in\"/>\n"
"      <arg type=\"o\" name=\"handle\" direction=\"out\"/>\n"
"    </method>\n"
"    <method name=\"SaveFile\">\n"
"      <arg type=\"s\" name=\"parent_window\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"title\" direction=\"in\"/>\n"
"      <arg type=\"a{sv}\" name=\"options\" direction=\"in\"/>\n"
"      <arg type=\"o\" name=\"handle\" direction=\"out\"/>\n"
"    </method>\n"
"    <property name=\"version\" type=\"u\" access=\"read\"/>\n"
"  </interface>\n"
"  <interface name=\"org.freedesktop.portal.OpenURI\">\n"
"    <method name=\"OpenURI\">\n"
"      <arg type=\"s\" name=\"parent_window\" direction=\"in\"/>\n"
"      <arg type=\"s\" name=\"uri\" direction=\"in\"/>\n"
"      <arg type=\"a{sv}\" name=\"options\" direction=\"in\"/>\n"
"      <arg type=\"o\" name=\"handle\" direction=\"out\"/>\n"
"    </method>\n"
"    <property name=\"version\" type=\"u\" access=\"read\"/>\n"
"  </interface>\n"
"  <node name=\"request\"/>\n"
"</node>\n";

static const char request_xml[] =
"<!DOCTYPE node PUBLIC \"-//freedesktop//DTD D-BUS Object Introspection 1.0//EN\"\n"
" \"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd\">\n"
"<node>\n"
"  <interface name=\"org.freedesktop.portal.Request\">\n"
"    <method name=\"Close\"/>\n"
"    <signal name=\"Response\">\n"
"      <arg type=\"u\" name=\"response\"/>\n"
"      <arg type=\"a{sv}\" name=\"results\"/>\n"
"    </signal>\n"
"  </interface>\n"
"</node>\n";

static void send_xml(wc_portal *p, const wc_msg *m, const char *xml)
{
    wc_writer w;

    wc_w_init(&w);
    wc_w_string(&w, xml);
    wc_bus_reply(&p->bus, m, "s", &w);
    wc_w_free(&w);
}

void wc_introspect_handle(wc_portal *p, const wc_msg *m)
{
    if (!strcmp(m->path, WC_PORTAL_PATH))
        send_xml(p, m, desktop_xml);
    else if (!strncmp(m->path, WC_REQUEST_PREFIX, sizeof WC_REQUEST_PREFIX - 1))
        send_xml(p, m, request_xml);
    else
        wc_bus_error(&p->bus, m, WC_ERR_UNKNOWN_OBJ, "no such object: %s",
                     m->path);
}