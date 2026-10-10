'use strict';
// Free, keyless web APIs. Only these hosts are ever contacted, over https.
const { net } = require('electron');

const WALLPAPER_LIST = 'https://picsum.photos/v2/list';
const GEO = 'https://ipwho.is/';
const WEATHER = 'https://api.open-meteo.com/v1/forecast';
const MAX_IMAGE = 6 * 1024 * 1024;

async function getJson(url, ms = 8000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try {
    const r = await net.fetch(url, { signal: ctl.signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return await r.json();
  } finally { clearTimeout(t); }
}

// Picks a random landscape photo from Lorem Picsum and returns it as a data URL (the page CSP allows data: only).
async function wallpaper(width = 1920, height = 1080) {
  const list = await getJson(`${WALLPAPER_LIST}?page=${1 + Math.floor(Math.random() * 10)}&limit=30`);
  const pick = list.filter((p) => p.width > p.height)[Math.floor(Math.random() * 10)] || list[0];
  if (!pick || !/^\d+$/.test(String(pick.id))) throw new Error('No wallpaper returned');
  const r = await net.fetch(`https://picsum.photos/id/${pick.id}/${width}/${height}.jpg`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  if (!/^image\/jpeg/.test(r.headers.get('content-type') || '')) throw new Error('Unexpected content type');
  const buf = Buffer.from(await r.arrayBuffer());
  if (buf.length > MAX_IMAGE) throw new Error('Image too large');
  return { id: String(pick.id), author: String(pick.author || 'unknown'), source: 'picsum.photos', dataUrl: `data:image/jpeg;base64,${buf.toString('base64')}` };
}

let cache = null;
// Approximate location from the public IP, then current weather. Opt-in: it reveals your IP to both services.
async function outside() {
  if (cache && Date.now() - cache.at < 30 * 60 * 1000) return cache.value;
  const g = await getJson(GEO);
  if (!g.success) throw new Error('Geolocation failed');
  const w = await getJson(`${WEATHER}?latitude=${g.latitude}&longitude=${g.longitude}&current=temperature_2m,weather_code,wind_speed_10m`);
  const value = { city: g.city, region: g.region, country: g.country, ip: g.ip, tempC: w.current.temperature_2m, wind: w.current.wind_speed_10m, code: w.current.weather_code, tz: g.timezone?.id || null };
  cache = { at: Date.now(), value };
  return value;
}

module.exports = { wallpaper, outside };
