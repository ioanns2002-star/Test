import { defineConfig } from 'vite';
import { resolve } from 'node:path';

export default defineConfig({
  root: resolve(import.meta.dirname),
  publicDir: resolve(import.meta.dirname, '../public'),
  base: './',
  build: {
    outDir: resolve(import.meta.dirname, '../dist'),
    emptyOutDir: true,
    assetsDir: 'assets'
  }
});
