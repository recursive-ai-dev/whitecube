'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');

const load = () => import('../../watermark.ts');

function image(w, h, stamp) {
  const d = new Uint8ClampedArray(w * h * 4);
  for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
    const i = (y * w + x) * 4, v = 90 + (x / w) * 60; // soft gradient, no hard edges
    d[i] = v; d[i + 1] = v; d[i + 2] = v; d[i + 3] = 255;
  }
  if (stamp) {
    const [x0, y0, x1, y1] = stamp;
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) {
      const on = ((x >> 1) + (y >> 1)) % 2 === 0, i = (y * w + x) * 4;
      d[i] = d[i + 1] = d[i + 2] = on ? 255 : 0;
    }
  }
  return d;
}

test('clean image is not flagged', async () => {
  const { detectWatermark } = await load();
  assert.equal(detectWatermark(image(400, 300, null), 400, 300).flagged, false);
});

test('dense stamp in bottom-right corner is flagged', async () => {
  const { detectWatermark } = await load();
  const r = detectWatermark(image(400, 300, [300, 262, 400, 300]), 400, 300);
  assert.equal(r.flagged, true);
  assert.equal(r.side, 'bottom-right');
});

test('123% zoom crops the watermark side away', async () => {
  const { coverRect, WATERMARK_ZOOM } = await load();
  assert.equal(WATERMARK_ZOOM, 1.23);
  const plain = coverRect(1920, 1080, 1920, 1080, 1, null);
  assert.equal(Math.round(plain.sw), 1920);
  const z = coverRect(1920, 1080, 1920, 1080, 1.23, 'bottom-right');
  assert.ok(Math.abs(z.sw - 1920 / 1.23) < 0.01);
  assert.equal(z.sy, 0);  // keeps the top, drops the bottom band
  assert.equal(z.sx, 0);  // keeps the left, drops the right edge
  assert.ok(z.sy + z.sh <= 1080 - 1080 * 0.14); // bottom 14% band is gone
});
