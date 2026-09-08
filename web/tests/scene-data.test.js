import assert from 'node:assert/strict';
import test from 'node:test';
import {
  advanceRainSegments,
  asDataUrl,
  decodeBase64,
  decodeEmbeddedManifest,
  imagePathCandidates,
  shouldKeepRenderLoop,
  validateSceneManifest
} from '../src/scene-data.js';

const manifest = {
  title: 'Rain gallery',
  version: '1.0',
  camera: {
    position: [1, 2, 3],
    target: [0, 1, 0],
    fov: 45,
    near: 0.1,
    far: 300
  },
  bounds: {
    min: [-5, 0, -8],
    max: [5, 8, 8]
  },
  assets: {
    model: 'assets/model.glb',
    reference: 'assets/reference.jpg',
    beauty: 'assets/beauty.webp'
  },
  stats: { objects: 12 }
};

test('validates and copies a scene manifest', () => {
  const validated = validateSceneManifest(manifest);

  assert.deepEqual(validated.camera.position, [1, 2, 3]);
  assert.notEqual(validated.camera.position, manifest.camera.position);
  assert.equal(validated.assets.model, 'assets/model.glb');
});

test('allows a deliberately locked walk axis but rejects inverted or empty bounds', () => {
  const lockedHeight = validateSceneManifest({
    ...manifest,
    bounds: { min: [-5, 1.65, -8], max: [5, 1.65, 8] }
  });

  assert.deepEqual(lockedHeight.bounds, { min: [-5, 1.65, -8], max: [5, 1.65, 8] });
  assert.throws(
    () => validateSceneManifest({ ...manifest, bounds: { min: [0, 0, 0], max: [-1, 2, 2] } }),
    /bounds\.max/
  );
  assert.throws(
    () => validateSceneManifest({ ...manifest, bounds: { min: [0, 0, 0], max: [0, 0, 0] } }),
    /пространство/
  );
});

test('decodes embedded JSON and image payloads', () => {
  const encoded = Buffer.from(JSON.stringify(manifest)).toString('base64');
  const bytes = decodeBase64('AQID');

  assert.deepEqual(decodeEmbeddedManifest(encoded), manifest);
  assert.deepEqual([...bytes], [1, 2, 3]);
  assert.equal(asDataUrl('AQID', 'image/webp'), 'data:image/webp;base64,AQID');
  assert.equal(asDataUrl('data:image/png;base64,AQID', 'image/webp'), 'data:image/png;base64,AQID');
});

test('offers only local same-name image alternatives for optional previews', () => {
  assert.deepEqual(imagePathCandidates('/assets/beauty.webp'), [
    '/assets/beauty.webp',
    '/assets/beauty.png',
    '/assets/beauty.jpg',
    '/assets/beauty.jpeg',
    '/assets/beauty.avif'
  ]);
  assert.deepEqual(imagePathCandidates('assets/reference.jpg?revision=4'), [
    'assets/reference.jpg?revision=4',
    'assets/reference.webp?revision=4',
    'assets/reference.png?revision=4',
    'assets/reference.jpeg?revision=4',
    'assets/reference.avif?revision=4'
  ]);
  assert.deepEqual(imagePathCandidates('https://example.test/render.webp'), ['https://example.test/render.webp']);
  assert.deepEqual(imagePathCandidates('data:image/png;base64,AQID'), ['data:image/png;base64,AQID']);
});

test('wraps rain segments independently without changing their streak lengths', () => {
  const positions = new Float32Array([
    1, 0.9, 3, 1, 0.6, 3,
    2, 0.1, 4, 2, -0.2, 4
  ]);
  const speeds = new Float32Array([1, 1]);

  const returned = advanceRainSegments(positions, speeds, 0.25, 0, 1);

  assert.equal(returned, positions);
  assert.ok(Math.abs(positions[1] - 0.65) < 0.00001);
  assert.ok(Math.abs(positions[4] - 0.35) < 0.00001);
  assert.ok(Math.abs(positions[7] - 0.85) < 0.00001);
  assert.ok(Math.abs(positions[10] - 0.55) < 0.00001);
  assert.ok(Math.abs((positions[1] - positions[4]) - 0.3) < 0.00001);
  assert.ok(Math.abs((positions[7] - positions[10]) - 0.3) < 0.00001);
});

test('keeps WebGL frames on demand rather than when the scene is idle or hidden', () => {
  const idle = {
    isReady: true,
    isPageVisible: true,
    motionEnabled: false,
    rainEnabled: false,
    hasRain: true,
    hasAnimations: false,
    isWalking: false,
    pendingFrames: 0
  };

  assert.equal(shouldKeepRenderLoop(idle), false);
  assert.equal(shouldKeepRenderLoop({ ...idle, pendingFrames: 1 }), true);
  assert.equal(shouldKeepRenderLoop({ ...idle, motionEnabled: true, rainEnabled: true }), true);
  assert.equal(shouldKeepRenderLoop({ ...idle, motionEnabled: true, hasAnimations: true }), true);
  assert.equal(shouldKeepRenderLoop({ ...idle, isWalking: true }), true);
  assert.equal(shouldKeepRenderLoop({ ...idle, isPageVisible: false, pendingFrames: 4 }), false);
});

test('accepts optional exported lighting only when its values are usable', () => {
  const validated = validateSceneManifest({
    ...manifest,
    lighting: {
      fogColor: '#718793',
      fogNear: 10,
      fogFar: 170,
      exitLight: [0.8, 2.46, -18],
      stepLights: [[0.24, 0.38, -7.8], ['not', 'a', 'vector']]
    }
  });

  assert.deepEqual(validated.lighting, {
    fogColor: '#718793',
    fogNear: 10,
    fogFar: 170,
    exitLight: [0.8, 2.46, -18],
    stepLights: [[0.24, 0.38, -7.8]]
  });
  assert.equal(validateSceneManifest({ ...manifest, lighting: { fogNear: 20, fogFar: 10 } }).lighting, null);
});
