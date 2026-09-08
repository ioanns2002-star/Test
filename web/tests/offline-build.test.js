import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import test from 'node:test';

test('offline build preserves replacement tokens and safely embeds script terminators', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'rain-gallery-offline-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const directory of ['scripts', 'dist/assets', 'public/assets', 'public/downloads']) {
    await mkdir(join(root, directory), { recursive: true });
  }
  await copyFile(new URL('../../scripts/build_offline.mjs', import.meta.url), join(root, 'scripts/build_offline.mjs'));

  const tokens = "$& $` $' $1 $$";
  const payload = { tokens, closingTag: '</ScRiPt><script>not executable</script>' };
  const manifest = { title: tokens, description: '</script><script>not executable</script>' };
  const javascript = `globalThis.result = ${JSON.stringify(payload)};`;
  const css = `.test::after { content: ${JSON.stringify(tokens)}; }`;
  const files = {
    'dist/index.html': '<!doctype html><html><head><title>Gallery</title><script type="module" src="./assets/main.js"></script><link rel="stylesheet" href="./assets/main.css"></head><body><main>SCENE</main><a href="downloads/rain-gallery.blend">Blender</a><a href="downloads/godot-gallery.zip">Godot</a></body></html>',
    'dist/assets/main.js': javascript,
    'dist/assets/main.css': css,
    'public/assets/scene-manifest.json': JSON.stringify(manifest),
    'public/assets/rain-gallery.glb': Buffer.from([0, 1, 2, 255]),
    'public/assets/reference.jpg': Buffer.from([3, 4, 5]),
    'public/assets/beauty.webp': Buffer.from([6, 7, 8])
  };
  for (const [path, content] of Object.entries(files)) {
    await writeFile(join(root, path), content);
  }

  execFileSync(process.execPath, [join(root, 'scripts/build_offline.mjs')], { timeout: 30_000 });
  const html = await readFile(join(root, 'public/downloads/gallery-offline.html'), 'utf8');
  const modules = [...html.matchAll(/<script type="module">([\s\S]*?)<\/script>/gi)];
  assert.equal(modules.length, 1);
  assert.equal([...html.matchAll(/<\/script>/gi)].length, 2);
  assert.doesNotMatch(html, /<script\b[^>]*src=|<link\b[^>]*rel="stylesheet"/i);
  assert.equal(html.match(/<style>([\s\S]*?)<\/style>/)[1], css);
  assert.equal((html.match(/<main>SCENE<\/main>/g) || []).length, 1);
  for (const file of ['rain-gallery.blend', 'godot-gallery.zip']) {
    assert.ok(html.includes(`href="https://github.com/ioanns2002-star/Test/blob/rain-gallery-v1/public/downloads/${file}" target="_blank" rel="noopener"`));
  }

  const context = { window: {} };
  const embeddedScript = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  runInNewContext(embeddedScript, context);
  runInNewContext(modules[0][1], context);
  assert.equal(JSON.stringify(context.result), JSON.stringify(payload));
  assert.equal(JSON.stringify(context.window.__SCENE_EMBED__.manifest), JSON.stringify(manifest));
  assert.deepEqual(Buffer.from(context.window.__SCENE_EMBED__.glbBase64, 'base64'), files['public/assets/rain-gallery.glb']);
});
