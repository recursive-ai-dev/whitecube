import React, { useEffect, useRef } from 'react';
import { WATERMARK_ZOOM, coverRect, detectWatermark, WatermarkSide } from '../watermark';

export interface WallpaperState { dataUrl: string; zoomed: boolean; side: WatermarkSide | null; score: number }

// Fixed full-window canvas behind the UI. Watermarked images are drawn at 123% anchored away from the mark.
export async function prepareWallpaper(dataUrl: string): Promise<WallpaperState> {
  const img = new Image();
  img.src = dataUrl;
  await img.decode();
  const probe = document.createElement('canvas');
  const pw = 320, ph = Math.max(1, Math.round((320 * img.naturalHeight) / img.naturalWidth));
  probe.width = pw; probe.height = ph;
  const pctx = probe.getContext('2d', { willReadFrequently: true });
  if (!pctx) return { dataUrl, zoomed: false, side: null, score: 0 };
  pctx.drawImage(img, 0, 0, pw, ph);
  const r = detectWatermark(pctx.getImageData(0, 0, pw, ph).data, pw, ph);
  return { dataUrl, zoomed: r.flagged, side: r.side, score: r.score };
}

const WallpaperCanvas: React.FC<{ wallpaper: WallpaperState | null }> = ({ wallpaper }) => {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let cancelled = false;
    const img = new Image();
    const draw = () => {
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(window.innerWidth * dpr);
      canvas.height = Math.round(window.innerHeight * dpr);
      ctx.fillStyle = '#04070d';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      if (!wallpaper || !img.complete || !img.naturalWidth) return;
      const r = coverRect(img.naturalWidth, img.naturalHeight, canvas.width, canvas.height, wallpaper.zoomed ? WATERMARK_ZOOM : 1, wallpaper.side);
      ctx.drawImage(img, r.sx, r.sy, r.sw, r.sh, 0, 0, canvas.width, canvas.height);
      // Smoked glass: darken and tint toward the whitecube background so red text stays legible.
      ctx.fillStyle = 'rgba(4, 7, 13, 0.7)';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    };
    if (wallpaper) { img.src = wallpaper.dataUrl; img.decode().then(() => { if (!cancelled) draw(); }).catch(() => {}); }
    draw();
    window.addEventListener('resize', draw);
    return () => { cancelled = true; window.removeEventListener('resize', draw); };
  }, [wallpaper]);

  return <canvas ref={ref} aria-hidden="true" data-zoomed={wallpaper?.zoomed ? '1.23' : '1'} className="fixed inset-0 w-full h-full -z-10 pointer-events-none" />;
};

export default WallpaperCanvas;
