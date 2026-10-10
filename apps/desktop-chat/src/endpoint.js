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
  const result = { ...settings };
  let url;
  try { url = new URL(String(result.endpoint || '').trim()); }
  catch { throw new Error('Enter a valid http:// or https:// endpoint.'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('Use an HTTP(S) base URL without credentials, query, or fragment.');
  }
  result.endpoint = url.href.replace(/\/+$/, '').replace(/\/chat\/completions$/, '');
  result.model = String(result.model || '').trim();
  if (!result.model) throw new Error('Enter a model name.');
  return result;
}

module.exports = { isLocalEndpoint, needsApiKey, validateSettings };
