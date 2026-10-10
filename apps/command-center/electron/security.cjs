'use strict';
const { pathToFileURL } = require('node:url');

function trustedUrl(url, index, devUrl) {
  try {
    const target = new URL(url);
    if (devUrl) return target.origin === new URL(devUrl).origin;
    return target.protocol === 'file:' && target.pathname === new URL(pathToFileURL(index).href).pathname && !target.search;
  } catch { return false; }
}
function validateDevUrl(value) {
  if (!value) return null;
  const u = new URL(value);
  if (u.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(u.hostname) || u.username || u.password) throw new Error('Development server must use HTTP on localhost');
  return u.href;
}
module.exports = { trustedUrl, validateDevUrl };
