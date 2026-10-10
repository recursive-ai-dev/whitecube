import { MessageBus, systemBus } from 'dbus-next';
import type { SysctlDbusClient, SysctlEntry, SysctlBackup } from './dbusClient.js';

const BUS_NAME = 'ca.smddev.CommandCenter1';
const OBJECT_PATH = '/ca/smddev/CommandCenter1/Sysctl';
const INTERFACE_NAME = 'ca.smddev.CommandCenter1.Sysctl';

export async function createRealDbusClient(bus: MessageBus = systemBus()): Promise<SysctlDbusClient> {
  const proxy = await bus.getProxyObject(BUS_NAME, OBJECT_PATH);
  const iface = proxy.getInterface(INTERFACE_NAME);

  return {
    async list(): Promise<SysctlEntry[]> {
      const rows: [string, string, string, boolean][] = await iface.List();
      return rows.map(([key, value, sourceFile, isManaged]) => ({ key, value, sourceFile, isManaged }));
    },
    async setManaged(entries): Promise<string> {
      return iface.SetManaged(entries.map((e) => [e.key, e.value]));
    },
    async listBackups(): Promise<SysctlBackup[]> {
      const rows: [number, [string, string][]][] = await iface.ListBackups();
      return rows.map(([timestamp, entries]) => ({
        timestamp,
        entries: entries.map(([key, value]) => ({ key, value })),
      }));
    },
    async restoreBackup(timestamp): Promise<string> {
      return iface.RestoreBackup(timestamp);
    },
  };
}
