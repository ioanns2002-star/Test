export const DEFAULT_ASSETS = Object.freeze({
  model: 'assets/rain-gallery.glb',
  reference: 'assets/reference.jpg',
  beauty: 'assets/beauty.webp'
});

const IMAGE_EXTENSIONS = Object.freeze(['.webp', '.png', '.jpg', '.jpeg', '.avif']);

const isFiniteNumber = (value) => typeof value === 'number' && Number.isFinite(value);

function ensureVector(value, name) {
  if (!Array.isArray(value) || value.length !== 3 || !value.every(isFiniteNumber)) {
    throw new Error(`Поле «${name}» должно содержать три конечных числа.`);
  }

  return value;
}

function ensureNonEmptyString(value, name) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`Поле «${name}» должно быть непустой строкой.`);
  }

  return value;
}

function optionalVector(value) {
  return Array.isArray(value) && value.length === 3 && value.every(isFiniteNumber) ? [...value] : null;
}

function normalizeLighting(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null;
  }

  const lighting = {};

  if (typeof value.fogColor === 'string' && /^(?:#[0-9a-f]{3}|#[0-9a-f]{6})$/i.test(value.fogColor)) {
    lighting.fogColor = value.fogColor;
  }

  if (isFiniteNumber(value.fogNear) && isFiniteNumber(value.fogFar) && value.fogNear >= 0 && value.fogFar > value.fogNear) {
    lighting.fogNear = value.fogNear;
    lighting.fogFar = value.fogFar;
  }

  const exitLight = optionalVector(value.exitLight);
  if (exitLight) {
    lighting.exitLight = exitLight;
  }

  if (Array.isArray(value.stepLights)) {
    const stepLights = value.stepLights.map(optionalVector).filter(Boolean);
    if (stepLights.length) {
      lighting.stepLights = stepLights;
    }
  }

  return Object.keys(lighting).length ? lighting : null;
}

/**
 * Validates the small, deliberately explicit scene-manifest contract.
 * Coordinates must already use Three.js axes: X right, Y up, Z back.
 */
export function validateSceneManifest(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('scene-manifest.json должен быть JSON-объектом.');
  }

  const title = ensureNonEmptyString(value.title, 'title');
  const version = ensureNonEmptyString(String(value.version ?? ''), 'version');
  const camera = value.camera;
  const bounds = value.bounds;

  if (!camera || typeof camera !== 'object') {
    throw new Error('В манифесте отсутствует объект «camera».');
  }

  if (!bounds || typeof bounds !== 'object') {
    throw new Error('В манифесте отсутствует объект «bounds».');
  }

  const position = ensureVector(camera.position, 'camera.position');
  const target = ensureVector(camera.target, 'camera.target');
  const min = ensureVector(bounds.min, 'bounds.min');
  const max = ensureVector(bounds.max, 'bounds.max');

  if (!max.every((entry, index) => entry >= min[index])) {
    throw new Error('Ни одна координата bounds.max не может быть меньше bounds.min.');
  }

  if (max.every((entry, index) => entry === min[index])) {
    throw new Error('bounds должен оставлять пространство хотя бы по одной оси.');
  }

  if (!isFiniteNumber(camera.fov) || camera.fov <= 1 || camera.fov >= 170) {
    throw new Error('camera.fov должен быть числом от 1 до 170.');
  }

  if (!isFiniteNumber(camera.near) || !isFiniteNumber(camera.far) || camera.near <= 0 || camera.far <= camera.near) {
    throw new Error('camera.near и camera.far должны задавать корректный диапазон камеры.');
  }

  const suppliedAssets = value.assets && typeof value.assets === 'object' ? value.assets : {};
  const assets = {};

  for (const [key, defaultPath] of Object.entries(DEFAULT_ASSETS)) {
    const asset = suppliedAssets[key] ?? defaultPath;
    assets[key] = ensureNonEmptyString(asset, `assets.${key}`);
  }

  return {
    ...value,
    title,
    version,
    camera: {
      ...camera,
      position: [...position],
      target: [...target]
    },
    bounds: {
      ...bounds,
      min: [...min],
      max: [...max]
    },
    assets,
    stats: value.stats && typeof value.stats === 'object' ? value.stats : null,
    lighting: normalizeLighting(value.lighting)
  };
}

export function decodeBase64(value) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error('Встроенный ресурс должен быть непустой Base64-строкой.');
  }

  const base64 = value.includes(',') ? value.slice(value.indexOf(',') + 1) : value;
  const normalized = base64.replace(/\s/g, '').replace(/-/g, '+').replace(/_/g, '/');

  if (typeof atob === 'function') {
    const binary = atob(normalized);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  }

  // Node exposes Buffer, while browser builds take the atob branch above.
  if (typeof Buffer !== 'undefined') {
    return new Uint8Array(Buffer.from(normalized, 'base64'));
  }

  throw new Error('Base64-декодер недоступен в этом окружении.');
}

export function decodeEmbeddedManifest(value) {
  if (value && typeof value === 'object') {
    return value;
  }

  if (typeof value !== 'string') {
    throw new Error('window.__SCENE_EMBED__.manifest должен быть объектом, JSON или Base64 JSON.');
  }

  try {
    return JSON.parse(value);
  } catch {
    const bytes = decodeBase64(value);
    const json = new TextDecoder().decode(bytes);
    return JSON.parse(json);
  }
}

export function asDataUrl(value, mimeType) {
  if (typeof value !== 'string' || !value.trim()) {
    return null;
  }

  return value.startsWith('data:') ? value : `data:${mimeType};base64,${value}`;
}

/**
 * Produces local, same-name image alternatives for a static preview only.
 * The GLB and manifest are never substituted.
 */
export function imagePathCandidates(path) {
  if (typeof path !== 'string' || !path.trim()) {
    return [];
  }

  // Remote and in-memory assets are explicit resources, not filenames to guess.
  if (/^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(path)) {
    return [path];
  }

  const match = /^(.*?)(\.(?:webp|png|jpe?g|avif))([?#].*)?$/i.exec(path);
  if (!match) {
    return [path];
  }

  const [, stem, suppliedExtension, suffix = ''] = match;
  const extension = suppliedExtension.toLowerCase();
  return [path, ...IMAGE_EXTENSIONS
    .filter((candidateExtension) => candidateExtension !== extension)
    .map((candidateExtension) => `${stem}${candidateExtension}${suffix}`)];
}

/**
 * Advances individual rain line segments through a repeating vertical volume.
 * Keeping each segment's phase independent prevents a visible empty interval.
 */
export function advanceRainSegments(positions, speeds, deltaSeconds, minY, maxY) {
  if (!ArrayBuffer.isView(positions) || positions.length % 6 !== 0) {
    throw new Error('Позиции дождя должны содержать пары трёхмерных точек.');
  }

  const count = positions.length / 6;
  if (!ArrayBuffer.isView(speeds) || speeds.length < count) {
    throw new Error('Для каждой капли дождя нужна скорость.');
  }

  if (![deltaSeconds, minY, maxY].every(isFiniteNumber) || deltaSeconds < 0 || maxY <= minY) {
    throw new Error('Нужен корректный временной шаг и диапазон дождя.');
  }

  const span = maxY - minY;
  for (let index = 0; index < count; index += 1) {
    const speed = speeds[index];
    if (!isFiniteNumber(speed) || speed < 0) {
      throw new Error('Скорость дождя должна быть неотрицательным конечным числом.');
    }

    const offset = index * 6;
    const previousHeadY = positions[offset + 1];
    const distanceFromMin = previousHeadY - minY - speed * deltaSeconds;
    const wrappedHeadY = ((distanceFromMin % span) + span) % span + minY;
    const translation = wrappedHeadY - previousHeadY;
    positions[offset + 1] = wrappedHeadY;
    positions[offset + 4] += translation;
  }

  return positions;
}

/**
 * Determines whether a frame is useful instead of keeping an idle WebGL loop alive.
 */
export function shouldKeepRenderLoop({
  isReady = false,
  isPageVisible = false,
  motionEnabled = false,
  rainEnabled = false,
  hasRain = false,
  hasAnimations = false,
  isWalking = false,
  pendingFrames = 0
} = {}) {
  if (!isReady || !isPageVisible) {
    return false;
  }

  return Boolean(
    pendingFrames > 0 ||
    isWalking ||
    (motionEnabled && ((rainEnabled && hasRain) || hasAnimations))
  );
}

export function formatCount(value) {
  return isFiniteNumber(value) ? new Intl.NumberFormat('ru-RU').format(value) : '—';
}
