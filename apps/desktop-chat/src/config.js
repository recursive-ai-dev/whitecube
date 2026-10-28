const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { app, safeStorage } = require('electron');
const { validateSettings } = require('./endpoint');

const DEFAULTS = {
  endpoint: 'https://api.openai.com/v1',
  model: 'gpt-4o-mini',
  apiKeyPlain: '',
  apiKeyEncrypted: '',
  confirmDestructive: true,
  toolsEnabled: true,
  temperature: 0.7,
  maxTokens: 8192,
  hotkey: 'CommandOrControl+Shift+T',
  chatSource: 'api',
  checkpointId: '',
  modelAliases: {}
};

function configPath() {
  return path.join(app.getPath('userData'), 'config.json');
}

function canEncrypt() {
  return safeStorage.isEncryptionAvailable() &&
    (!safeStorage.getSelectedStorageBackend || safeStorage.getSelectedStorageBackend() !== 'basic_text');
}

function load() {
  const p = configPath();
  let raw = {};
  if (fs.existsSync(p)) {
    try {
      raw = JSON.parse(fs.readFileSync(p, 'utf8'));
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) raw = {};
    } catch (e) {
      raw = {};
    }
  }
  const cfg = { ...DEFAULTS, ...raw };

  let apiKey = '';
  if (cfg.apiKeyEncrypted && safeStorage.isEncryptionAvailable()) {
    try {
      apiKey = safeStorage.decryptString(Buffer.from(cfg.apiKeyEncrypted, 'base64'));
    } catch (e) {
      apiKey = '';
    }
  } else if (cfg.apiKeyPlain) {
    apiKey = cfg.apiKeyPlain;
  }

  return {
    endpoint: cfg.endpoint,
    model: cfg.model,
    apiKey,
    confirmDestructive: typeof cfg.confirmDestructive === 'boolean' ? cfg.confirmDestructive : true,
    toolsEnabled: typeof cfg.toolsEnabled === 'boolean' ? cfg.toolsEnabled : true,
    temperature: typeof cfg.temperature === 'number' && Number.isFinite(cfg.temperature) && cfg.temperature >= 0 && cfg.temperature <= 2 ? cfg.temperature : DEFAULTS.temperature,
    maxTokens: Number.isInteger(cfg.maxTokens) && cfg.maxTokens >= 1 && cfg.maxTokens <= 32768 ? cfg.maxTokens : DEFAULTS.maxTokens,
    hotkey: typeof cfg.hotkey === 'string' ? cfg.hotkey : DEFAULTS.hotkey,
    keyStorage: canEncrypt() ? 'encrypted' : 'plaintext',
    chatSource: cfg.chatSource === 'local' ? 'local' : 'api',
    checkpointId: typeof cfg.checkpointId === 'string' ? cfg.checkpointId : '',
    modelAliases: cfg.modelAliases && typeof cfg.modelAliases === 'object' && !Array.isArray(cfg.modelAliases) ? cfg.modelAliases : {}
  };
}

function save(settings) {
  if (!settings || typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Settings must be an object.');
  if (settings.apiKey !== undefined && typeof settings.apiKey !== 'string') throw new Error('API key must be text.');
  let existingRaw = {};
  try {
    existingRaw = JSON.parse(fs.readFileSync(configPath(), 'utf8') || '{}');
  } catch (e) {
    existingRaw = {}; // missing or corrupt: start fresh instead of refusing to save
  }

  const out = { ...DEFAULTS, ...existingRaw };
  for (const key of ['endpoint', 'model', 'confirmDestructive', 'toolsEnabled', 'temperature', 'maxTokens', 'hotkey', 'chatSource', 'checkpointId', 'modelAliases']) {
    if (settings[key] !== undefined) out[key] = settings[key];
  }
  const validated = validateSettings(out);
  if (typeof out.confirmDestructive !== 'boolean') throw new Error('Confirmation preference must be true or false.');
  if (typeof out.hotkey !== 'string' || out.hotkey.length > 128) throw new Error('Invalid hotkey.');
  out.endpoint = validated.endpoint; out.model = validated.model;
  if (!['api', 'local'].includes(out.chatSource)) throw new Error('Choose an API model or local checkpoint.');
  if (typeof out.checkpointId !== 'string') throw new Error('Invalid checkpoint selection.');
  if (!out.modelAliases || typeof out.modelAliases !== 'object' || Array.isArray(out.modelAliases)) throw new Error('Invalid checkpoint names.');

  if (typeof settings.apiKey === 'string') {
    validateSettings({ ...out, apiKey: settings.apiKey });
    if (canEncrypt() && settings.apiKey) {
      out.apiKeyEncrypted = safeStorage.encryptString(settings.apiKey).toString('base64');
      out.apiKeyPlain = '';
    } else {
      out.apiKeyPlain = settings.apiKey;
      out.apiKeyEncrypted = '';
    }
  }

  const p = configPath();
  fs.mkdirSync(path.dirname(p), { recursive: true, mode: 0o700 });
  const temporary = `${p}.new-${crypto.randomUUID()}`;
  try {
    fs.writeFileSync(temporary, JSON.stringify(out, null, 2), { mode: 0o600, flag: 'wx' });
    fs.renameSync(temporary, p);
  } finally { try { fs.unlinkSync(temporary); } catch {} }
  return load();
}

module.exports = { load, save, configPath };
