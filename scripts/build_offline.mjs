import { readFile, writeFile } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const dist = resolve(root, 'dist');
let html = await readFile(resolve(dist, 'index.html'), 'utf8');
const manifest = JSON.parse(await readFile(resolve(root, 'public/assets/scene-manifest.json'), 'utf8'));
const embed = {
  manifest,
  glbBase64: (await readFile(resolve(root, 'public/assets/rain-gallery.glb'))).toString('base64'),
  referenceBase64: (await readFile(resolve(root, 'public/assets/reference.jpg'))).toString('base64'),
  referenceMimeType: 'image/jpeg',
  beautyBase64: (await readFile(resolve(root, 'public/assets/beauty.webp'))).toString('base64'),
  beautyMimeType: 'image/webp'
};
const scriptTag = /<script\b[^>]*src="([^"]+\.js)"[^>]*><\/script>/g;
const scripts = [...html.matchAll(scriptTag)];
if (scripts.length !== 1) throw new Error(`Expected one self-contained JS bundle, got ${scripts.length}`);
for (const [tag, path] of scripts) {
  const js = await readFile(resolve(dist, path.replace(/^\.\//, '')), 'utf8');
  html = html.replace(tag, '');
  const safeJson = JSON.stringify(embed).replace(/</g, '\\u003c');
  const safeScript = js.replace(/<\/script/gi, (match) => match.replace('/', '\\/'));
  // A callback keeps $` and other replacement tokens inside bundled code literal.
  html = html.replace('</body>', () => `<script>window.__SCENE_EMBED__=${safeJson};</script>\n<script type="module">${safeScript}</script>\n</body>`);
}
for (const [tag, path] of [...html.matchAll(/<link\b[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>/g)]) {
  const css = await readFile(resolve(dist, path.replace(/^\.\//, '')), 'utf8');
  html = html.replace(tag, () => `<style>${css}</style>`);
}
const github = 'https://github.com/ioanns2002-star/Test/blob/rain-gallery-v1/public/downloads/';
html = html.replace(/href="(?:\.?\/?downloads\/)(rain-gallery\.blend|godot-gallery\.zip|gallery-offline\.html)"/g, (_, name) => `href="${github}${name}" target="_blank" rel="noopener"`);
html = html.replace('<title>', '<title>Offline · ');
if (/<script\b[^>]*src=|<link\b[^>]*rel="stylesheet"/i.test(html)) throw new Error('Offline page still depends on external JS or CSS');
const target = resolve(root, 'public/downloads/gallery-offline.html');
await writeFile(target, html);
console.log(`Created single-file offline viewer: ${(Buffer.byteLength(html) / 1024 / 1024).toFixed(1)} MiB`);
