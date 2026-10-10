import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// file:// pages get no response headers, so the production CSP is baked into index.html.
const prodCsp = {
  name: 'prod-csp',
  apply: 'build' as const,
  transformIndexHtml: () => [{
    tag: 'meta',
    attrs: {
      'http-equiv': 'Content-Security-Policy',
      content: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'",
    },
    injectTo: 'head-prepend' as const,
  }],
};

export default defineConfig({
  base: './',
  server: { port: 3000, host: '127.0.0.1', strictPort: true },
  plugins: [react(), prodCsp],
  build: { outDir: 'dist', emptyOutDir: true },
  resolve: { alias: { '@': path.resolve(__dirname, '.') } },
});
