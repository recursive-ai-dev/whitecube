// Pure helpers about the configured endpoint (no electron imports, so node can test them).

// Servers on this machine (LM Studio, Ollama, llama.cpp) normally take no API key.
function isLocalEndpoint(endpoint) {
  let host;
  try {
    host = new URL(String(endpoint || '').trim()).hostname.toLowerCase();
  } catch (e) {
    return false;
  }
  host = host.replace(/^\[|\]$/g, '');
  return host === 'localhost' || host.endsWith('.localhost') || host === '::1' || /^127(\.\d{1,3}){3}$/.test(host);
}

function needsApiKey(endpoint) {
  return !isLocalEndpoint(endpoint);
}

function validateSettings(settings) {
  if (!settings || typeof settings !== 'object' || Array.isArray(settings)) throw new Error('Settings must be an object.');
  const result = { ...settings };
  if (typeof result.endpoint !== 'string' || result.endpoint.length > 2048 || /[\x00-\x20\x7f]/.test(result.endpoint.trim())) throw new Error('Enter a valid http:// or https:// endpoint.');
  let url;
  try { url = new URL(String(result.endpoint || '').trim()); }
  catch { throw new Error('Enter a valid http:// or https:// endpoint.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('Use an HTTP(S) base URL without credentials, query, or fragment.');
  }
  result.endpoint = url.href.replace(/\/+$/, '').replace(/\/chat\/completions$/, '');
  if (typeof result.model !== 'string' || result.model.length > 256 || /[\x00-\x1f\x7f]/.test(result.model)) throw new Error('Enter a valid model name of up to 256 characters.');
  result.model = result.model.trim();
  if (!result.model) throw new Error('Enter a model name.');
  if (result.apiKey !== undefined && (typeof result.apiKey !== 'string' || result.apiKey.length > 16384 || /[\r\n\x00]/.test(result.apiKey))) throw new Error('Enter a valid API key without line breaks.');
  if (result.toolsEnabled !== undefined && typeof result.toolsEnabled !== 'boolean') throw new Error('File tools preference must be true or false.');
  if (result.temperature !== undefined && (typeof result.temperature !== 'number' || !Number.isFinite(result.temperature) || result.temperature < 0 || result.temperature > 2)) throw new Error('Temperature must be a number between 0 and 2.');
  if (result.maxTokens !== undefined && (!Number.isInteger(result.maxTokens) || result.maxTokens < 1 || result.maxTokens > 32768)) throw new Error('Response limit must be a whole number between 1 and 32768.');
  return result;
}

module.exports = { isLocalEndpoint, needsApiKey, validateSettings };
