// Watermark detection and the 123% canvas zoom used to push a watermark off the wallpaper.
export const WATERMARK_ZOOM = 1.23;

export type WatermarkSide = 'bottom' | 'bottom-left' | 'bottom-right' | 'top-left' | 'top-right';
export interface WatermarkResult { flagged: boolean; score: number; side: WatermarkSide | null }

const EDGE = 60; // luma step that counts as a hard edge

const edgeMap = (rgba: Uint8ClampedArray, w: number, h: number): Uint8Array => {
  const luma = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) luma[i] = 0.2126 * rgba[i * 4] + 0.7152 * rgba[i * 4 + 1] + 0.0722 * rgba[i * 4 + 2];
  const edges = new Uint8Array(w * h);
  for (let y = 0; y < h - 1; y++) {
    for (let x = 0; x < w - 1; x++) {
      const i = y * w + x;
      if (Math.abs(luma[i] - luma[i + 1]) > EDGE || Math.abs(luma[i] - luma[i + w]) > EDGE) edges[i] = 1;
    }
  }
  return edges;
};

const density = (edges: Uint8Array, w: number, x0: number, y0: number, x1: number, y1: number): number => {
  let n = 0;
  for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) n += edges[y * w + x];
  return n / Math.max(1, (x1 - x0) * (y1 - y0));
};

// A watermark is a small, dense cluster of hard edges in a corner or bottom band, much busier
// than the picture's centre. Natural photos have edges too, so the ratio against the centre matters.
export function detectWatermark(rgba: Uint8ClampedArray, w: number, h: number): WatermarkResult {
  const edges = edgeMap(rgba, w, h);
  const cw = Math.floor(w * 0.3), ch = Math.floor(h * 0.14);
  const centre = density(edges, w, Math.floor(w * 0.3), Math.floor(h * 0.3), Math.floor(w * 0.7), Math.floor(h * 0.7));
  const regions: [WatermarkSide, number][] = [
    ['bottom-left', density(edges, w, 0, h - ch, cw, h)],
    ['bottom-right', density(edges, w, w - cw, h - ch, w, h)],
    ['bottom', density(edges, w, Math.floor((w - cw) / 2), h - ch, Math.floor((w + cw) / 2), h)],
    ['top-left', density(edges, w, 0, 0, cw, ch)],
    ['top-right', density(edges, w, w - cw, 0, w, ch)],
  ];
  const [side, best] = regions.reduce((a, b) => (b[1] > a[1] ? b : a));
  const score = best / (centre + 0.005);
  const flagged = best > 0.08 && score > 2.5;
  return { flagged, score, side: flagged ? side : null };
}

// Source rectangle to draw so the image covers dw x dh at `zoom`, anchored away from the watermark.
export function coverRect(sw: number, sh: number, dw: number, dh: number, zoom = 1, side: WatermarkSide | null = null) {
  const scale = Math.max(dw / sw, dh / sh) * zoom;
  const vw = dw / scale, vh = dh / scale;
  const ax = side === 'bottom-left' || side === 'top-left' ? 1 : side === 'bottom-right' || side === 'top-right' ? 0 : 0.5;
  const ay = side === 'top-left' || side === 'top-right' ? 1 : side ? 0 : 0.5;
  return { sx: (sw - vw) * ax, sy: (sh - vh) * ay, sw: vw, sh: vh };
}
