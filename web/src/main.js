import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import {
  advanceRainSegments,
  asDataUrl,
  decodeBase64,
  decodeEmbeddedManifest,
  formatCount,
  imagePathCandidates,
  shouldKeepRenderLoop,
  validateSceneManifest
} from './scene-data.js';
import './styles.css';

const $ = (selector) => {
  const element = document.querySelector(selector);

  if (!element) {
    throw new Error(`Не найден обязательный элемент интерфейса: ${selector}`);
  }

  return element;
};

const dom = {
  canvas: $('#scene-canvas'),
  sceneTitle: $('#scene-title'),
  sceneVersion: $('#scene-version'),
  sceneStateBadge: $('#scene-state-badge'),
  sceneStateLabel: $('#scene-state-label'),
  viewStatus: $('#view-status'),
  assetStatus: $('#asset-status'),
  controlPanel: $('#control-panel'),
  loadingCard: $('#loading-card'),
  loadingMessage: $('#loading-message'),
  loadingDetail: $('#loading-detail'),
  loadingProgress: $('#loading-progress'),
  errorCard: $('#error-card'),
  errorMessage: $('#error-message'),
  staticFallback: $('#static-fallback'),
  fallbackImage: $('#fallback-image'),
  fallbackDescription: $('#fallback-description'),
  exposure: $('#exposure'),
  exposureValue: $('#exposure-value'),
  rainToggle: $('#rain-toggle'),
  motionToggle: $('#motion-toggle'),
  bloomToggle: $('#bloom-toggle'),
  motionDescription: $('#motion-description'),
  resetView: $('#reset-view'),
  screenshot: $('#screenshot'),
  fullscreen: $('#fullscreen'),
  helpButton: $('#help-button'),
  closeHelp: $('#close-help'),
  helpDrawer: $('#help-drawer'),
  comparisonButton: $('#comparison-button'),
  closeComparison: $('#close-comparison'),
  comparisonPanel: $('#comparison-panel'),
  comparisonSplit: $('#comparison-split'),
  comparisonSliderView: $('#comparison-slider-view'),
  comparisonRange: $('#comparison-range'),
  sliderOverlay: $('#slider-overlay'),
  sliderHandle: $('#slider-handle'),
  referenceImage: $('#reference-image'),
  beautyImage: $('#beauty-image'),
  referenceFileLabel: $('#reference-file-label'),
  beautyFileLabel: $('#beauty-file-label'),
  sliderReferenceImage: $('#slider-reference-image'),
  sliderBeautyImage: $('#slider-beauty-image'),
  downloadButton: $('#download-button'),
  downloadMenu: $('#download-menu'),
  modelDownload: $('[data-download="model"]'),
  beautyDownload: $('[data-download="beauty"]'),
  toast: $('#toast')
};

const EMBED_KEY = '__SCENE_EMBED__';
const MANIFEST_TIMEOUT_MS = 15_000;
const MODEL_TIMEOUT_MS = 60_000;
const motionMedia = window.matchMedia('(prefers-reduced-motion: reduce)');
const SCENE_STATES = Object.freeze({
  loading: {
    label: 'ЗАГРУЗКА 3D · GLB',
    title: 'Загружаем интерактивную GLB-сцену.'
  },
  realtime: {
    label: 'REALTIME · GLB',
    title: 'Это интерактивная GLB-сцена, не статический Blender-рендер.'
  },
  fallback: {
    label: '3D НЕДОСТУПЕН · НЕ REALTIME',
    title: '3D-сцена не открылась. Показан статический Blender-рендер, если файл доступен.'
  }
});

const state = {
  renderer: null,
  scene: null,
  camera: null,
  orbit: null,
  walk: null,
  composer: null,
  bloomPass: null,
  pmrem: null,
  environmentTarget: null,
  root: null,
  rain: null,
  mixer: null,
  manifest: null,
  source: null,
  mode: 'orbit',
  matchingView: true,
  applyingView: false,
  rainEnabled: true,
  bloomEnabled: true,
  motionEnabled: !motionMedia.matches,
  motionExplicitlySet: false,
  loadGeneration: 0,
  animationFrame: 0,
  pendingFrames: 0,
  lastFrameTime: performance.now(),
  isReady: false,
  isPageVisible: !document.hidden,
  sceneState: 'loading',
  lastError: null,
  orbitChangeHandler: null,
  toastTimer: 0
};

class SceneLoadError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = 'SceneLoadError';
    this.cause = cause;
  }
}

class WalkController {
  constructor(camera, canvas, bounds, onMove) {
    this.camera = camera;
    this.canvas = canvas;
    this.bounds = bounds;
    this.onMove = onMove;
    this.enabled = false;
    this.dragging = false;
    this.lastPointer = new THREE.Vector2();
    this.keys = new Set();
    this.yaw = 0;
    this.pitch = 0;
    this.speed = Math.max(2.1, Math.min(4.8, bounds.getSize(new THREE.Vector3()).length() * 0.13));

    this.onPointerDown = this.onPointerDown.bind(this);
    this.onPointerMove = this.onPointerMove.bind(this);
    this.onPointerUp = this.onPointerUp.bind(this);
    this.onKeyDown = this.onKeyDown.bind(this);
    this.onKeyUp = this.onKeyUp.bind(this);
    this.onBlur = this.onBlur.bind(this);

    canvas.addEventListener('pointerdown', this.onPointerDown);
    canvas.addEventListener('pointermove', this.onPointerMove);
    canvas.addEventListener('pointerup', this.onPointerUp);
    canvas.addEventListener('pointercancel', this.onPointerUp);
    window.addEventListener('keydown', this.onKeyDown);
    window.addEventListener('keyup', this.onKeyUp);
    window.addEventListener('blur', this.onBlur);
  }

  setEnabled(enabled) {
    this.enabled = enabled;
    this.canvas.classList.toggle('is-walk-mode', enabled);
    if (!enabled) {
      this.dragging = false;
      this.keys.clear();
    }
  }

  syncFromCamera(target) {
    const direction = new THREE.Vector3().subVectors(target, this.camera.position).normalize();
    this.yaw = Math.atan2(-direction.x, -direction.z);
    this.pitch = Math.asin(THREE.MathUtils.clamp(direction.y, -1, 1));
  }

  onPointerDown(event) {
    if (!this.enabled || event.button !== 0) {
      return;
    }

    this.canvas.focus({ preventScroll: true });
    this.dragging = true;
    this.lastPointer.set(event.clientX, event.clientY);
    this.canvas.classList.add('is-dragging');

    try {
      this.canvas.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is a convenience, not a requirement for navigation.
    }

    event.preventDefault();
  }

  onPointerMove(event) {
    if (!this.enabled || !this.dragging) {
      return;
    }

    const dx = event.clientX - this.lastPointer.x;
    const dy = event.clientY - this.lastPointer.y;
    this.lastPointer.set(event.clientX, event.clientY);
    this.yaw -= dx * 0.0032;
    this.pitch = THREE.MathUtils.clamp(this.pitch - dy * 0.0026, -1.42, 1.42);
    this.applyOrientation();
    this.onMove();
    event.preventDefault();
  }

  onPointerUp(event) {
    if (!this.dragging) {
      return;
    }

    this.dragging = false;
    this.canvas.classList.remove('is-dragging');

    if (event?.pointerId !== undefined && this.canvas.hasPointerCapture?.(event.pointerId)) {
      this.canvas.releasePointerCapture(event.pointerId);
    }
  }

  onKeyDown(event) {
    if (!this.enabled || document.activeElement !== this.canvas || !['KeyW', 'KeyA', 'KeyS', 'KeyD'].includes(event.code)) {
      return;
    }

    const keyAdded = !this.keys.has(event.code);
    this.keys.add(event.code);
    if (keyAdded) this.onMove();
    event.preventDefault();
  }

  onKeyUp(event) {
    if (this.keys.delete(event.code)) this.onMove();
  }

  onBlur() {
    this.keys.clear();
    this.dragging = false;
    this.canvas.classList.remove('is-dragging');
  }

  applyOrientation() {
    this.camera.quaternion.setFromEuler(new THREE.Euler(this.pitch, this.yaw, 0, 'YXZ'));
  }

  get isMoving() {
    return this.enabled && this.keys.size > 0;
  }

  update(delta) {
    if (!this.enabled || this.keys.size === 0) {
      return;
    }

    const input = new THREE.Vector2(
      Number(this.keys.has('KeyD')) - Number(this.keys.has('KeyA')),
      Number(this.keys.has('KeyW')) - Number(this.keys.has('KeyS'))
    );

    if (input.lengthSq() === 0) {
      return;
    }

    input.normalize();
    const forward = new THREE.Vector3(-Math.sin(this.yaw), 0, -Math.cos(this.yaw));
    const right = new THREE.Vector3(Math.cos(this.yaw), 0, -Math.sin(this.yaw));
    const movement = forward.multiplyScalar(input.y).add(right.multiplyScalar(input.x));
    this.camera.position.addScaledVector(movement, this.speed * delta);

    const clampAxis = (value, min, max) => {
      const span = max - min;
      if (span <= 0) return min;

      const inset = Math.min(0.2, span / 2);
      return THREE.MathUtils.clamp(value, min + inset, max - inset);
    };

    this.camera.position.x = clampAxis(this.camera.position.x, this.bounds.min.x, this.bounds.max.x);
    this.camera.position.y = clampAxis(this.camera.position.y, this.bounds.min.y, this.bounds.max.y);
    this.camera.position.z = clampAxis(this.camera.position.z, this.bounds.min.z, this.bounds.max.z);
    this.onMove();
  }

  dispose() {
    this.canvas.removeEventListener('pointerdown', this.onPointerDown);
    this.canvas.removeEventListener('pointermove', this.onPointerMove);
    this.canvas.removeEventListener('pointerup', this.onPointerUp);
    this.canvas.removeEventListener('pointercancel', this.onPointerUp);
    window.removeEventListener('keydown', this.onKeyDown);
    window.removeEventListener('keyup', this.onKeyUp);
    window.removeEventListener('blur', this.onBlur);
  }
}

function projectUrl(path) {
  if (typeof path !== 'string') {
    return null;
  }

  if (/^(?:https?:|data:|blob:)/i.test(path)) {
    return path;
  }

  const base = new URL(import.meta.env.BASE_URL, window.location.href);
  return new URL(path.replace(/^\/+/, ''), base).href;
}

function mimeFromPath(path, fallback) {
  const lower = String(path ?? '').toLowerCase();

  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  if (lower.endsWith('.avif')) return 'image/avif';
  if (lower.endsWith('.webp')) return 'image/webp';
  return fallback;
}

function filenameFromPath(path, fallback) {
  if (typeof path !== 'string') {
    return fallback;
  }

  const filename = path.split('/').pop()?.split(/[?#]/)[0];
  return filename || fallback;
}

function timeoutAfter(milliseconds, message) {
  let timer;
  const promise = new Promise((_, reject) => {
    timer = window.setTimeout(() => reject(new SceneLoadError(message)), milliseconds);
  });

  return {
    promise,
    clear: () => window.clearTimeout(timer)
  };
}

async function fetchManifest(url) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), MANIFEST_TIMEOUT_MS);

  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    if (!response.ok) {
      throw new SceneLoadError(`Файл scene-manifest.json не найден или недоступен (${response.status}).`);
    }

    return validateSceneManifest(await response.json());
  } catch (error) {
    if (error.name === 'AbortError') {
      throw new SceneLoadError(`Манифест не ответил за ${MANIFEST_TIMEOUT_MS / 1000} секунд.`);
    }
    if (error instanceof SceneLoadError) {
      throw error;
    }
    throw new SceneLoadError('Не удалось прочитать scene-manifest.json. Проверьте JSON и путь /assets/scene-manifest.json.', error);
  } finally {
    window.clearTimeout(timer);
  }
}

function embeddedImageSource(embed, key, mimeType) {
  const base64Key = `${key}Base64`;
  return asDataUrl(embed[base64Key], embed[`${key}MimeType`] ?? mimeType);
}

function previewAssetCandidates(path, embeddedUrl = null) {
  if (embeddedUrl) {
    return [{
      url: embeddedUrl,
      path,
      filename: filenameFromPath(path, 'встроенное изображение'),
      embedded: true
    }];
  }

  return imagePathCandidates(path).map((candidatePath) => ({
    url: projectUrl(candidatePath),
    path: candidatePath,
    filename: filenameFromPath(candidatePath, 'изображение'),
    embedded: false
  }));
}

async function getSceneSource() {
  const embed = window[EMBED_KEY];

  if (embed) {
    if (!embed.manifest || !embed.glbBase64) {
      throw new SceneLoadError('Оффлайн-встраивание найдено, но требует manifest и glbBase64.');
    }

    let manifest;
    let modelBytes;

    try {
      manifest = validateSceneManifest(decodeEmbeddedManifest(embed.manifest));
      modelBytes = decodeBase64(embed.glbBase64);
    } catch (error) {
      throw new SceneLoadError(`Не удалось разобрать встроенную сцену: ${error.message}`, error);
    }

    return {
      manifest,
      modelBytes,
      modelUrl: null,
      modelDownloadUrl: asDataUrl(embed.glbBase64, 'model/gltf-binary'),
      referenceImages: previewAssetCandidates(
        manifest.assets.reference,
        embeddedImageSource(embed, 'reference', mimeFromPath(manifest.assets.reference, 'image/jpeg'))
      ),
      beautyImages: previewAssetCandidates(
        manifest.assets.beauty,
        embeddedImageSource(embed, 'beauty', mimeFromPath(manifest.assets.beauty, 'image/webp'))
      ),
      embedded: true
    };
  }

  const manifest = await fetchManifest(projectUrl('assets/scene-manifest.json'));
  return {
    manifest,
    modelBytes: null,
    modelUrl: projectUrl(manifest.assets.model),
    modelDownloadUrl: null,
    referenceImages: previewAssetCandidates(manifest.assets.reference),
    beautyImages: previewAssetCandidates(manifest.assets.beauty),
    embedded: false
  };
}

function loadGltf(source) {
  const loader = new GLTFLoader();
  const timeout = timeoutAfter(MODEL_TIMEOUT_MS, `GLB не загрузился за ${MODEL_TIMEOUT_MS / 1000} секунд. Проверьте размер файла и соединение.`);

  const loading = new Promise((resolve, reject) => {
    const onProgress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        const percent = Math.min(91, 15 + (event.loaded / event.total) * 76);
        setLoading(percent, `Загружаем геометрию · ${Math.round((event.loaded / event.total) * 100)}%`, 'GLB остаётся интерактивной сценой после загрузки.');
      } else {
        setLoading(28, 'Загружаем геометрию…', 'Сервер не передал размер GLB; ожидание ограничено одной минутой.');
      }
    };

    const onError = (error) => reject(new SceneLoadError('Не удалось открыть rain-gallery.glb. Проверьте экспорт glTF/GLB и путь к файлу.', error));

    if (source.modelBytes) {
      loader.parse(source.modelBytes.buffer, '', resolve, onError);
    } else {
      loader.load(source.modelUrl, resolve, onProgress, onError);
    }
  });

  return Promise.race([loading, timeout.promise]).finally(timeout.clear);
}

function setLoading(progress, message, detail) {
  dom.loadingCard.hidden = false;
  dom.errorCard.hidden = true;
  dom.loadingProgress.style.width = `${THREE.MathUtils.clamp(progress, 0, 100)}%`;
  dom.loadingMessage.textContent = message;
  dom.loadingDetail.textContent = detail;
}

function setAssetStatus(message) {
  dom.assetStatus.textContent = message;
}

function updateMetadata(manifest) {
  dom.sceneTitle.textContent = manifest.title;
  dom.sceneVersion.textContent = `v${manifest.version}`;
  document.title = `${manifest.title} — 3D reconstruction`;
}

function setSceneState(status) {
  const resolvedStatus = Object.hasOwn(SCENE_STATES, status) ? status : 'loading';
  const sceneState = SCENE_STATES[resolvedStatus];
  state.sceneState = resolvedStatus;
  dom.sceneStateBadge.classList.remove('is-loading', 'is-realtime', 'is-fallback');
  dom.sceneStateBadge.classList.add(`is-${resolvedStatus}`);
  dom.sceneStateLabel.textContent = sceneState.label;
  dom.sceneStateBadge.title = sceneState.title;
  dom.sceneStateBadge.setAttribute('aria-label', sceneState.title);
}

function configureImage(image, candidates, { onResolved, onMissing } = {}) {
  const frame = image.closest('.comparison-image-wrap');
  frame?.classList.remove('is-missing');
  image.removeAttribute('src');

  if (!candidates?.length) {
    frame?.classList.add('is-missing');
    onMissing?.();
    return;
  }

  const loadId = `${performance.now()}-${Math.random()}`;
  image.dataset.loadId = loadId;
  let index = 0;

  const loadNext = () => {
    const candidate = candidates[index];
    index += 1;

    if (!candidate) {
      frame?.classList.add('is-missing');
      onMissing?.();
      return;
    }

    image.onload = () => {
      if (image.dataset.loadId !== loadId) return;
      frame?.classList.remove('is-missing');
      onResolved?.(candidate);
    };
    image.onerror = () => {
      if (image.dataset.loadId === loadId) loadNext();
    };
    image.src = candidate.url;
  };

  loadNext();
}

function previewLabel(candidate, fallback) {
  if (!candidate) {
    return `${fallback} · не realtime`;
  }

  return `${candidate.filename}${candidate.embedded ? ' · встроено' : ''} · не realtime`;
}

function setDownloadLink(link, url, path) {
  if (!url) {
    link.removeAttribute('href');
    link.setAttribute('aria-disabled', 'true');
    return;
  }

  link.href = url;
  const filename = filenameFromPath(path, 'scene-download');
  link.download = filename;
  link.removeAttribute('aria-disabled');
  const extension = filename.match(/(\.[a-z0-9]+)$/i)?.[1];
  const extensionLabel = link.querySelector('small');
  if (extension && extensionLabel) extensionLabel.textContent = extension;
}

function setBeautyAsset(candidate) {
  dom.beautyFileLabel.textContent = previewLabel(candidate, 'beauty');
  if (candidate) {
    setDownloadLink(dom.beautyDownload, candidate.url, candidate.path);
  }
}

function configurePreviewImages(source) {
  const reference = source.referenceImages[0];
  const beauty = source.beautyImages[0];
  dom.referenceFileLabel.textContent = previewLabel(reference, 'reference');
  setBeautyAsset(beauty);

  configureImage(dom.referenceImage, source.referenceImages, {
    onResolved: (candidate) => {
      dom.referenceFileLabel.textContent = previewLabel(candidate, 'reference');
    }
  });
  configureImage(dom.sliderReferenceImage, source.referenceImages);
  configureImage(dom.beautyImage, source.beautyImages, { onResolved: setBeautyAsset });
  configureImage(dom.sliderBeautyImage, source.beautyImages);

  dom.fallbackImage.hidden = false;
  dom.fallbackDescription.textContent = 'Показан статический Blender-рендер, а не интерактивная сцена.';
  configureImage(dom.fallbackImage, source.beautyImages, {
    onResolved: setBeautyAsset,
    onMissing: () => {
      dom.fallbackImage.hidden = true;
      dom.fallbackDescription.textContent = '3D-сцена и статический Blender-рендер сейчас недоступны.';
    }
  });
}

function configureDownloadLinks(source) {
  setDownloadLink(
    dom.modelDownload,
    source.modelDownloadUrl ?? source.modelUrl,
    source.manifest.assets.model
  );
  const beauty = source.beautyImages[0];
  setDownloadLink(dom.beautyDownload, beauty?.url, beauty?.path ?? source.manifest.assets.beauty);
}

function referencePose() {
  const { camera } = state.manifest;
  return {
    position: new THREE.Vector3().fromArray(camera.position),
    target: new THREE.Vector3().fromArray(camera.target)
  };
}

function applyReferenceView() {
  if (!state.camera || !state.manifest) {
    return;
  }

  const pose = referencePose();
  state.applyingView = true;
  state.camera.fov = state.manifest.camera.fov;
  state.camera.near = state.manifest.camera.near;
  state.camera.far = state.manifest.camera.far;
  state.camera.position.copy(pose.position);
  state.camera.lookAt(pose.target);
  state.camera.updateProjectionMatrix();

  if (state.orbit) {
    state.orbit.target.copy(pose.target);
    state.orbit.update();
  }

  state.walk?.syncFromCamera(pose.target);
  state.matchingView = true;
  state.applyingView = false;
  updateViewStatus();
  renderFrame();
}

function updateViewStatus() {
  if (!state.isReady) {
    dom.viewStatus.textContent = 'Подготовка сцены';
    return;
  }

  if (state.matchingView) {
    dom.viewStatus.textContent = 'Camera_Reference · исходный ракурс';
    return;
  }

  dom.viewStatus.textContent = state.mode === 'walk' ? 'Прогулка · пользовательский вид' : 'Орбита · пользовательский вид';
}

function markUserView() {
  if (!state.applyingView && state.isReady) {
    state.matchingView = false;
    updateViewStatus();
  }
}

function addSceneLights(bounds, lighting) {
  const size = bounds.getSize(new THREE.Vector3());
  const center = bounds.getCenter(new THREE.Vector3());
  const span = Math.max(size.x, size.y, size.z, 8);

  const overcast = new THREE.HemisphereLight(0xadb8c9, 0x263237, 0.40);
  state.scene.add(overcast);

  const skyLight = new THREE.DirectionalLight(0xbdc9d4, 1.0);
  skyLight.position.set(12, 18, 8);
  skyLight.target.position.copy(center);
  skyLight.castShadow = true;
  skyLight.shadow.mapSize.set(window.innerWidth < 720 ? 1024 : 2048, window.innerWidth < 720 ? 1024 : 2048);
  skyLight.shadow.camera.left = -span;
  skyLight.shadow.camera.right = span;
  skyLight.shadow.camera.top = span;
  skyLight.shadow.camera.bottom = -span;
  skyLight.shadow.camera.near = 0.1;
  skyLight.shadow.camera.far = span * 4;
  skyLight.shadow.bias = -0.00008;
  skyLight.shadow.normalBias = 0.014;
  skyLight.shadow.radius = 4;
  state.scene.add(skyLight, skyLight.target);

  const corridorFill = new THREE.PointLight(0xb4c0c5, 0.22, 10, 2);
  corridorFill.position.set(1.15, 2.05, .6);
  state.scene.add(corridorFill);

  if (lighting?.exitLight) {
    const exitLight = new THREE.PointLight(0xa9ce83, 0.12, 2.5, 2);
    exitLight.name = 'Exit_sign_light';
    exitLight.position.fromArray(lighting.exitLight);
    state.scene.add(exitLight);
  }

  lighting?.stepLights?.forEach((position, index) => {
    const stepLight = new THREE.PointLight(0xb1c794, 0.025, 1.2, 2);
    stepLight.name = `Step_light_${index + 1}`;
    stepLight.position.fromArray(position);
    state.scene.add(stepLight);
  });
}

function createRain(bounds) {
  const size = bounds.getSize(new THREE.Vector3());
  const center = bounds.getCenter(new THREE.Vector3());
  const mobile = window.innerWidth < 720;
  const count = mobile ? 250 : 650;
  const width = Math.max(size.x + 12, 16);
  const depth = Math.max(size.z + 14, 20);
  const height = Math.max(size.y + 10, 18);
  const baseY = bounds.min.y - 2;
  const positions = new Float32Array(count * 6);
  const speeds = new Float32Array(count);

  for (let index = 0; index < count; index += 1) {
    const offset = index * 6;
    const x = 2.5 + Math.random() * width;
    const y = baseY + Math.random() * height;
    const z = center.z + (Math.random() - 0.5) * depth;
    const length = 0.16 + Math.random() * 0.24;

    positions[offset] = x;
    positions[offset + 1] = y;
    positions[offset + 2] = z;
    positions[offset + 3] = x - 0.018;
    positions[offset + 4] = y - length;
    positions[offset + 5] = z + 0.006;
    speeds[index] = 4.6 + Math.random() * 2.7;
  }

  const geometry = new THREE.BufferGeometry();
  const positionAttribute = new THREE.BufferAttribute(positions, 3).setUsage(THREE.DynamicDrawUsage);
  geometry.setAttribute('position', positionAttribute);
  const material = new THREE.LineBasicMaterial({
    color: 0x9dbbc3,
    transparent: true,
    opacity: 0.13,
    depthWrite: false,
    blending: THREE.NormalBlending
  });
  const rain = new THREE.LineSegments(geometry, material);
  rain.name = 'Realtime_Rain';
  rain.frustumCulled = false;
  rain.visible = state.rainEnabled;
  state.scene.add(rain);

  return {
    object: rain,
    positions,
    positionAttribute,
    speeds,
    minY: baseY,
    maxY: baseY + height
  };
}

function setModelShadows(root) {
  root.traverse((object) => {
    if (!object.isMesh) {
      return;
    }

    object.castShadow = true;
    object.receiveShadow = true;
  });
}

function initializeRenderer(manifest) {
  if (!('WebGLRenderingContext' in window) && !('WebGL2RenderingContext' in window)) {
    throw new SceneLoadError('Этот браузер не предоставляет WebGL. Вместо сцены показан статический Blender-рендер.');
  }

  try {
    state.renderer = new THREE.WebGLRenderer({
      canvas: dom.canvas,
      antialias: true,
      alpha: false,
      powerPreference: 'high-performance',
      preserveDrawingBuffer: true
    });
  } catch (error) {
    throw new SceneLoadError('Не удалось инициализировать WebGL. Проверьте аппаратное ускорение браузера.', error);
  }

  state.renderer.outputColorSpace = THREE.SRGBColorSpace;
  state.renderer.toneMapping = THREE.AgXToneMapping;
  state.renderer.toneMappingExposure = Number(dom.exposure.value);
  state.renderer.shadowMap.enabled = true;
  state.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  state.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, window.innerWidth < 720 ? 1.5 : 2));
  state.renderer.setSize(window.innerWidth, window.innerHeight, false);

  state.scene = new THREE.Scene();
  state.scene.background = new THREE.Color(0x718397);
  const lighting = manifest.lighting;
  state.scene.fog = lighting?.fogColor && Number.isFinite(lighting.fogNear) && Number.isFinite(lighting.fogFar)
    ? new THREE.Fog(lighting.fogColor, lighting.fogNear, lighting.fogFar)
    : new THREE.FogExp2(0x0a171b, 0.025);

  state.camera = new THREE.PerspectiveCamera(manifest.camera.fov, window.innerWidth / window.innerHeight, manifest.camera.near, manifest.camera.far);
  const pose = referencePose();
  state.camera.position.copy(pose.position);
  state.camera.lookAt(pose.target);

  state.pmrem = new THREE.PMREMGenerator(state.renderer);
  const environment = new RoomEnvironment();
  state.environmentTarget = state.pmrem.fromScene(environment, 0.035);
  state.scene.environment = state.environmentTarget.texture;
  state.scene.environmentIntensity = 0.12;

  const bounds = new THREE.Box3(
    new THREE.Vector3().fromArray(manifest.bounds.min),
    new THREE.Vector3().fromArray(manifest.bounds.max)
  );
  addSceneLights(bounds, lighting);
  state.rain = createRain(bounds);

  state.orbit = new OrbitControls(state.camera, dom.canvas);
  state.orbit.enableDamping = true;
  state.orbit.dampingFactor = 0.075;
  state.orbit.enablePan = true;
  state.orbit.screenSpacePanning = true;
  state.orbit.zoomToCursor = true;
  const referenceDistance = pose.position.distanceTo(pose.target);
  state.orbit.minDistance = Math.max(0.1, Math.min(1, referenceDistance * 0.25));
  state.orbit.maxDistance = Math.max(referenceDistance * 4, bounds.getSize(new THREE.Vector3()).length() * 2.5, 12);
  state.orbit.target.copy(pose.target);
  state.orbitChangeHandler = () => {
    markUserView();
    requestRender();
  };
  state.orbit.addEventListener('change', state.orbitChangeHandler);
  state.orbit.update();

  state.walk = new WalkController(state.camera, dom.canvas, bounds, () => {
    markUserView();
    requestRender();
  });
  state.walk.syncFromCamera(pose.target);
  state.walk.setEnabled(false);

  state.composer = new EffectComposer(state.renderer);
  state.composer.addPass(new RenderPass(state.scene, state.camera));
  state.bloomPass = new UnrealBloomPass(
    new THREE.Vector2(window.innerWidth, window.innerHeight),
    0.10,
    0.40,
    1.30
  );
  state.bloomPass.enabled = state.bloomEnabled;
  state.composer.addPass(state.bloomPass);
  state.composer.addPass(new OutputPass());
}

function setupModel(gltf) {
  if (!gltf?.scene) {
    throw new SceneLoadError('GLB открыт, но не содержит сцену.');
  }

  state.root = gltf.scene;
  setModelShadows(state.root);
  state.scene.add(state.root);

  if (gltf.animations?.length) {
    state.mixer = new THREE.AnimationMixer(state.root);
    gltf.animations.forEach((clip) => state.mixer.clipAction(clip).play());
  }
}

function statsSummary(stats) {
  if (!stats) {
    return 'GLB загружен · параметры камеры из манифеста';
  }

  const labels = [
    ['objects', 'объектов'],
    ['editableObjects', 'объектов'],
    ['meshes', 'мешей'],
    ['triangles', 'треугольников'],
    ['materials', 'материалов']
  ];
  const parts = labels
    .filter(([key]) => Number.isFinite(stats[key]))
    .map(([key, label]) => `${formatCount(stats[key])} ${label}`);

  return parts.length ? `GLB · ${parts.join(' · ')}` : 'GLB загружен · параметры камеры из манифеста';
}

function updateMode(mode) {
  if (!['orbit', 'walk'].includes(mode)) {
    return;
  }

  state.mode = mode;
  document.querySelectorAll('[data-mode]').forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });

  if (state.orbit && state.walk && state.camera) {
    if (mode === 'walk') {
      const forward = new THREE.Vector3();
      state.camera.getWorldDirection(forward);
      state.orbit.enabled = false;
      state.walk.syncFromCamera(state.camera.position.clone().add(forward));
      state.walk.setEnabled(true);
    } else {
      const forward = new THREE.Vector3();
      state.camera.getWorldDirection(forward);
      state.applyingView = true;
      state.walk.setEnabled(false);
      state.orbit.target.copy(state.camera.position).addScaledVector(forward, Math.max(1, state.camera.position.distanceTo(state.orbit.target)));
      state.orbit.enabled = true;
      state.orbit.update();
      state.applyingView = false;
    }
  }

  updateViewStatus();
  showToast(mode === 'walk' ? 'Прогулка: тяните для обзора, затем W A S D.' : 'Режим орбиты включён.');
  requestRender(2);
}

function renderFrame() {
  if (!state.renderer || !state.scene || !state.camera) {
    return;
  }

  if (state.composer && state.bloomEnabled) {
    state.composer.render();
  } else {
    state.renderer.render(state.scene, state.camera);
  }
}

function queueAnimationFrame() {
  if (!state.isReady || !state.isPageVisible || state.animationFrame) {
    return;
  }

  state.lastFrameTime = performance.now();
  state.animationFrame = window.requestAnimationFrame(animate);
}

function requestRender(frames = 1) {
  if (!state.isReady || !state.isPageVisible) {
    return;
  }

  const requestedFrames = Number.isFinite(frames) ? Math.max(1, Math.ceil(frames)) : 1;
  state.pendingFrames = Math.max(state.pendingFrames, requestedFrames);
  queueAnimationFrame();
}

function shouldContinueRendering() {
  return shouldKeepRenderLoop({
    isReady: state.isReady,
    isPageVisible: state.isPageVisible,
    motionEnabled: state.motionEnabled,
    rainEnabled: state.rainEnabled,
    hasRain: Boolean(state.rain?.object?.visible),
    hasAnimations: Boolean(state.mixer),
    isWalking: Boolean(state.walk?.isMoving),
    pendingFrames: state.pendingFrames
  });
}

function animate(now) {
  state.animationFrame = 0;
  if (!state.isReady || !state.isPageVisible) {
    return;
  }

  const delta = Math.min(0.05, Math.max(0, (now - state.lastFrameTime) / 1000));
  state.lastFrameTime = now;

  state.orbit?.update();
  state.walk?.update(delta);

  if (state.motionEnabled) {
    state.mixer?.update(delta);
    if (state.rain?.object && state.rainEnabled) {
      advanceRainSegments(
        state.rain.positions,
        state.rain.speeds,
        delta,
        state.rain.minY,
        state.rain.maxY
      );
      state.rain.positionAttribute.needsUpdate = true;
    }
  }

  renderFrame();
  const keepRendering = shouldContinueRendering();
  state.pendingFrames = Math.max(0, state.pendingFrames - 1);
  if (keepRendering) queueAnimationFrame();
}

function beginRendering() {
  window.cancelAnimationFrame(state.animationFrame);
  state.animationFrame = 0;
  requestRender();
}

function resizeRenderer() {
  if (!state.renderer || !state.camera) {
    return;
  }

  const width = window.innerWidth;
  const height = window.innerHeight;
  state.camera.aspect = width / height;
  state.camera.updateProjectionMatrix();
  state.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, width < 720 ? 1.5 : 2));
  state.renderer.setSize(width, height, false);
  state.composer?.setSize(width, height);
  renderFrame();
}

function disposeTexture(texture, disposed) {
  if (!texture || disposed.has(texture)) {
    return;
  }

  disposed.add(texture);
  texture.dispose?.();
}

function disposeMaterial(material, disposedMaterials, disposedTextures) {
  if (!material || disposedMaterials.has(material)) {
    return;
  }

  disposedMaterials.add(material);
  Object.values(material).forEach((value) => {
    if (value?.isTexture) {
      disposeTexture(value, disposedTextures);
    }
  });
  Object.values(material.uniforms ?? {}).forEach((uniform) => {
    if (uniform?.value?.isTexture) {
      disposeTexture(uniform.value, disposedTextures);
    }
  });
  material.dispose?.();
}

function disposeModel(root) {
  if (!root) {
    return;
  }

  const disposedMaterials = new Set();
  const disposedTextures = new Set();
  root.traverse((object) => {
    object.geometry?.dispose?.();
    if (Array.isArray(object.material)) {
      object.material.forEach((material) => disposeMaterial(material, disposedMaterials, disposedTextures));
    } else {
      disposeMaterial(object.material, disposedMaterials, disposedTextures);
    }
  });
}

function teardownScene() {
  window.cancelAnimationFrame(state.animationFrame);
  state.animationFrame = 0;
  state.pendingFrames = 0;
  if (state.orbit && state.orbitChangeHandler) {
    state.orbit.removeEventListener('change', state.orbitChangeHandler);
  }
  state.orbit?.dispose();
  state.walk?.dispose();
  state.mixer?.stopAllAction();
  disposeModel(state.root);

  if (state.rain?.object) {
    state.rain.object.geometry.dispose();
    state.rain.object.material.dispose();
  }

  state.environmentTarget?.dispose();
  state.pmrem?.dispose();
  state.composer?.dispose?.();
  state.renderer?.renderLists?.dispose?.();
  state.renderer?.dispose();

  state.renderer = null;
  state.scene = null;
  state.camera = null;
  state.orbit = null;
  state.walk = null;
  state.composer = null;
  state.bloomPass = null;
  state.pmrem = null;
  state.environmentTarget = null;
  state.root = null;
  state.rain = null;
  state.mixer = null;
  state.orbitChangeHandler = null;
  state.isReady = false;
}

function errorText(error) {
  if (error instanceof SceneLoadError) {
    return error.message;
  }
  return 'Произошла непредвиденная ошибка при открытии сцены. Откройте консоль браузера для технических деталей.';
}

function showFailure(error) {
  console.error('[Rain Gallery]', error);
  teardownScene();
  state.isReady = false;
  state.lastError = errorText(error);
  setSceneState('fallback');
  dom.loadingCard.hidden = true;
  dom.errorCard.hidden = false;
  dom.errorMessage.textContent = errorText(error);
  dom.staticFallback.hidden = false;
  setAssetStatus('3D не открыт · доступен статический preview, если он загружен');
  updateViewStatus();
}

function showToast(message) {
  window.clearTimeout(state.toastTimer);
  dom.toast.textContent = message;
  dom.toast.classList.add('is-visible');
  state.toastTimer = window.setTimeout(() => dom.toast.classList.remove('is-visible'), 3200);
}

function setComparisonOpen(open) {
  dom.comparisonPanel.hidden = !open;
  dom.comparisonButton.setAttribute('aria-expanded', String(open));
  if (open) {
    dom.downloadMenu.hidden = true;
    dom.downloadButton.setAttribute('aria-expanded', 'false');
  }
}

function setCompareMode(mode) {
  const slider = mode === 'slider';
  dom.comparisonSplit.hidden = slider;
  dom.comparisonSliderView.hidden = !slider;
  document.querySelectorAll('[data-compare-mode]').forEach((button) => {
    const active = button.dataset.compareMode === mode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function updateComparisonSlider() {
  const value = Number(dom.comparisonRange.value);
  dom.sliderOverlay.style.clipPath = `inset(0 ${100 - value}% 0 0)`;
  dom.sliderHandle.style.left = `${value}%`;
}

async function takeScreenshot() {
  if (!state.isReady || !state.renderer) {
    showToast('Снимок будет доступен после загрузки интерактивной сцены.');
    return;
  }

  renderFrame();
  dom.canvas.toBlob((blob) => {
    if (!blob) {
      showToast('Браузер не смог создать PNG-снимок.');
      return;
    }

    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'rain-gallery-realtime.png';
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 500);
    showToast('PNG-снимок интерактивного вида скачивается.');
  }, 'image/png');
}

async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
    } else if (dom.canvas.parentElement?.requestFullscreen) {
      await dom.canvas.parentElement.requestFullscreen();
    } else {
      showToast('Полноэкранный режим не поддерживается этим браузером.');
    }
  } catch {
    showToast('Не удалось переключить полноэкранный режим.');
  }
}

function updateFullscreenLabel() {
  dom.fullscreen.textContent = document.fullscreenElement ? 'Выйти из экрана' : 'На весь экран';
}

function updateExposure() {
  const value = Number(dom.exposure.value);
  dom.exposureValue.value = value.toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (state.renderer) {
    state.renderer.toneMappingExposure = value;
    renderFrame();
  }
}

function setMotionFromPreference() {
  if (state.motionExplicitlySet) {
    return;
  }

  state.motionEnabled = !motionMedia.matches;
  dom.motionToggle.checked = state.motionEnabled;
  dom.motionDescription.textContent = motionMedia.matches ? 'выключено по reduced motion' : 'мягкая анимация';
  renderFrame();
  requestRender();
}

function getDiagnostics() {
  const rendererInfo = state.renderer?.info;
  return {
    sceneState: state.sceneState,
    ready: state.isReady,
    source: state.source ? (state.source.embedded ? 'embedded' : 'network') : null,
    lastError: state.lastError,
    renderLoop: {
      pageVisible: state.isPageVisible,
      queued: Boolean(state.animationFrame),
      pendingFrames: state.pendingFrames,
      continuous: shouldKeepRenderLoop({
        isReady: state.isReady,
        isPageVisible: state.isPageVisible,
        motionEnabled: state.motionEnabled,
        rainEnabled: state.rainEnabled,
        hasRain: Boolean(state.rain?.object?.visible),
        hasAnimations: Boolean(state.mixer),
        isWalking: Boolean(state.walk?.isMoving)
      })
    },
    rain: state.rain ? {
      enabled: state.rainEnabled,
      segments: state.rain.positions.length / 6,
      minY: state.rain.minY,
      maxY: state.rain.maxY
    } : null,
    renderer: rendererInfo ? {
      calls: rendererInfo.render?.calls ?? null,
      triangles: rendererInfo.render?.triangles ?? null,
      lines: rendererInfo.render?.lines ?? null,
      points: rendererInfo.render?.points ?? null,
      geometries: rendererInfo.memory?.geometries ?? null,
      textures: rendererInfo.memory?.textures ?? null
    } : null
  };
}

function handleWebglContextLost(event) {
  event.preventDefault();
  if (!state.renderer && !state.isReady) {
    return;
  }

  state.loadGeneration += 1;
  showFailure(new SceneLoadError('WebGL-контекст был потерян. Повторите загрузку сцены.'));
}

function bindInterface() {
  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => updateMode(button.dataset.mode));
  });

  dom.exposure.addEventListener('input', updateExposure);
  dom.rainToggle.addEventListener('change', () => {
    state.rainEnabled = dom.rainToggle.checked;
    if (state.rain?.object) state.rain.object.visible = state.rainEnabled;
    renderFrame();
    requestRender();
  });
  dom.motionToggle.checked = state.motionEnabled;
  dom.motionDescription.textContent = motionMedia.matches ? 'выключено по reduced motion' : 'мягкая анимация';
  dom.motionToggle.addEventListener('change', () => {
    state.motionExplicitlySet = true;
    state.motionEnabled = dom.motionToggle.checked;
    dom.motionDescription.textContent = state.motionEnabled ? 'мягкая анимация' : 'сцена остаётся статичной';
    renderFrame();
    requestRender();
  });
  dom.bloomToggle.addEventListener('change', () => {
    state.bloomEnabled = dom.bloomToggle.checked;
    if (state.bloomPass) state.bloomPass.enabled = state.bloomEnabled;
    renderFrame();
  });

  dom.resetView.addEventListener('click', () => {
    applyReferenceView();
    showToast('Восстановлен точный вид из scene-manifest.json.');
  });
  dom.screenshot.addEventListener('click', takeScreenshot);
  dom.fullscreen.addEventListener('click', toggleFullscreen);
  document.addEventListener('fullscreenchange', updateFullscreenLabel);

  dom.helpButton.addEventListener('click', () => {
    const open = dom.helpDrawer.hidden;
    dom.helpDrawer.hidden = !open;
    dom.helpButton.setAttribute('aria-expanded', String(open));
  });
  dom.closeHelp.addEventListener('click', () => {
    dom.helpDrawer.hidden = true;
    dom.helpButton.setAttribute('aria-expanded', 'false');
  });

  dom.comparisonButton.addEventListener('click', () => setComparisonOpen(dom.comparisonPanel.hidden));
  dom.closeComparison.addEventListener('click', () => setComparisonOpen(false));
  document.querySelectorAll('[data-compare-mode]').forEach((button) => {
    button.addEventListener('click', () => setCompareMode(button.dataset.compareMode));
  });
  dom.comparisonRange.addEventListener('input', updateComparisonSlider);

  dom.downloadButton.addEventListener('click', () => {
    const open = dom.downloadMenu.hidden;
    dom.downloadMenu.hidden = !open;
    dom.downloadButton.setAttribute('aria-expanded', String(open));
  });

  $('#retry-load').addEventListener('click', bootScene);
  $('#fallback-retry').addEventListener('click', bootScene);
  $('#error-show-compare').addEventListener('click', () => setComparisonOpen(true));

  document.addEventListener('pointerdown', (event) => {
    if (!event.target.closest('.bottom-actions')) {
      dom.downloadMenu.hidden = true;
      dom.downloadButton.setAttribute('aria-expanded', 'false');
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!dom.helpDrawer.hidden) {
      dom.helpDrawer.hidden = true;
      dom.helpButton.setAttribute('aria-expanded', 'false');
    }
    if (!dom.comparisonPanel.hidden) setComparisonOpen(false);
    if (!dom.downloadMenu.hidden) {
      dom.downloadMenu.hidden = true;
      dom.downloadButton.setAttribute('aria-expanded', 'false');
    }
  });

  window.addEventListener('resize', resizeRenderer);
  dom.canvas.addEventListener('webglcontextlost', handleWebglContextLost, false);
  document.addEventListener('visibilitychange', () => {
    state.isPageVisible = !document.hidden;
    state.lastFrameTime = performance.now();
    if (!state.isPageVisible) {
      window.cancelAnimationFrame(state.animationFrame);
      state.animationFrame = 0;
      return;
    }
    requestRender();
  });
  motionMedia.addEventListener?.('change', setMotionFromPreference);
}

async function bootScene() {
  const generation = ++state.loadGeneration;
  teardownScene();
  state.lastError = null;
  setSceneState('loading');
  dom.errorCard.hidden = true;
  dom.staticFallback.hidden = true;
  setLoading(4, 'Читаем манифест сцены…', 'Точный вид камеры берётся только из scene-manifest.json.');
  setAssetStatus('Проверяем /assets/scene-manifest.json…');

  try {
    const source = await getSceneSource();
    if (generation !== state.loadGeneration) return;

    state.manifest = source.manifest;
    state.source = source;
    updateMetadata(source.manifest);
    configurePreviewImages(source);
    configureDownloadLinks(source);
    setLoading(14, 'Манифест проверен', source.embedded ? 'Оффлайн-ресурсы найдены в window.__SCENE_EMBED__.' : 'Открываем GLB и настраиваем физический свет.');
    initializeRenderer(source.manifest);
    const gltf = await loadGltf(source);
    if (generation !== state.loadGeneration) return;

    setLoading(94, 'Собираем материалы и освещение…', 'Добавляем PBR-окружение, туман, тени и управляемый дождь.');
    setupModel(gltf);
    state.isReady = true;
    setSceneState('realtime');
    applyReferenceView();
    setLoading(100, 'Интерактивная сцена готова', 'Это realtime GLB. Статический Blender-рендер доступен только в сравнении.');
    setAssetStatus(statsSummary(source.manifest.stats));
    window.setTimeout(() => {
      if (generation === state.loadGeneration) dom.loadingCard.hidden = true;
    }, 440);
    beginRendering();
  } catch (error) {
    if (generation !== state.loadGeneration) return;
    showFailure(error);
  }
}

window.RainGalleryViewer = Object.freeze({
  reload: bootScene,
  getManifest: () => state.manifest,
  getDiagnostics,
  embedContract: Object.freeze({
    manifest: 'object | JSON string | Base64 JSON',
    glbBase64: 'Base64 GLB payload',
    referenceBase64: 'optional Base64 source image',
    beautyBase64: 'optional Base64 Blender render',
    referenceMimeType: 'optional MIME type, defaults from manifest path',
    beautyMimeType: 'optional MIME type, defaults from manifest path'
  })
});

bindInterface();
updateComparisonSlider();
setCompareMode('split');
bootScene();
