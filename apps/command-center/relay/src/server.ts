import express, { Express } from 'express';
import { SysctlDbusClient } from './dbusClient.js';

export function createServer(client: SysctlDbusClient): Express {
  const app = express();
  app.use(express.json());

  app.get('/api/sysctl', async (_req, res) => {
    try {
      const entries = await client.list();
      res.json({ entries });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.post('/api/sysctl/apply', async (req, res) => {
    const entries = req.body?.entries;
    if (!Array.isArray(entries) || entries.some((e) => typeof e?.key !== 'string' || typeof e?.value !== 'string')) {
      res.status(400).json({ error: 'body must be { entries: [{ key, value }] }' });
      return;
    }
    try {
      const output = await client.setManaged(entries);
      res.json({ output });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.get('/api/sysctl/backups', async (_req, res) => {
    try {
      const backups = await client.listBackups();
      res.json({ backups });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  app.post('/api/sysctl/backups/:timestamp/restore', async (req, res) => {
    const timestamp = Number(req.params.timestamp);
    if (!Number.isFinite(timestamp)) {
      res.status(400).json({ error: 'timestamp must be numeric' });
      return;
    }
    try {
      const output = await client.restoreBackup(timestamp);
      res.json({ output });
    } catch (err) {
      res.status(502).json({ error: (err as Error).message });
    }
  });

  return app;
}
