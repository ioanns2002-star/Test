# Rain Gallery viewer

This is a self-contained Vite/Three.js viewer. It renders the exported **GLB** in WebGL; the Blender beauty render is only used as a labeled static preview and comparison image.

## Runtime assets

Place these files under `public/` (the Vite config maps that directory to the site root):

```text
public/
  assets/
    rain-gallery.glb
    scene-manifest.json
    reference.jpg
    beauty.webp              # `beauty.png` is also supported when named in the manifest
  downloads/
    rain-gallery.blend
    godot-gallery.zip
    gallery-offline.html
```

`scene-manifest.json` is intentionally the source of truth for the matching camera. Coordinates already use Three.js/glTF axes: **X right, Y up, Z back**. The viewer does not infer or substitute the reference pose.

```json
{
  "title": "Дождливая галерея",
  "version": "1.0.0",
  "camera": {
    "position": [0, 1.65, 8.2],
    "target": [0, 1.55, -8.5],
    "fov": 43,
    "near": 0.1,
    "far": 160
  },
  "bounds": {
    "min": [-4, -0.1, -17],
    "max": [8, 12, 10]
  },
  "assets": {
    "model": "assets/rain-gallery.glb",
    "reference": "assets/reference.jpg",
    "beauty": "assets/beauty.webp"
  },
  "lighting": {
    "fogColor": "#718793",
    "fogNear": 10,
    "fogFar": 170,
    "exitLight": [0.8, 2.46, -18],
    "stepLights": [[0.24, 0.38, -7.8]]
  },
  "stats": {}
}
```

`stats` values are optional; omit unknown values rather than writing estimates. `lighting` is also optional: when valid exporter coordinates are supplied, the viewer uses its linear fog range and adds restrained fill lights for the exit sign and step lamps. The `assets` paths can be changed, including `beauty.png`.

`bounds` controls the walk camera volume. A matching `min`/`max` value is valid for a deliberately locked axis (for example, `y: 1.65` keeps the walkthrough at eye level); only inverted bounds or a volume locked on every axis are rejected.

For the two static comparison images only, a missing local image transparently tries the same filename with the other common image extensions (`.webp`, `.png`, `.jpg`, `.jpeg`, `.avif`). The visible filename and render download link update to the resource that actually loaded. This is deliberately limited to optional previews: the model, manifest, camera pose, and scene data are never guessed or substituted.

## Fully offline HTML

To make a single-file viewer, inject `window.__SCENE_EMBED__` **before** the compiled viewer module runs. The loader accepts a manifest object, JSON string, or Base64 JSON. For a genuinely offline page, include all three payloads:

```html
<script>
  window.__SCENE_EMBED__ = {
    manifest: { "title": "…", "version": "1.0.0", "camera": { "position": [0, 0, 0], "target": [0, 0, -1], "fov": 43, "near": 0.1, "far": 160 }, "bounds": { "min": [-1, -1, -1], "max": [1, 1, 1] } },
    glbBase64: "BASE64_ENCODED_RAIN_GALLERY_GLB",
    referenceBase64: "BASE64_ENCODED_REFERENCE_JPEG",
    referenceMimeType: "image/jpeg",
    beautyBase64: "BASE64_ENCODED_BEAUTY_WEBP",
    beautyMimeType: "image/webp"
  };
</script>
```

When this object is present, the GLB is parsed from memory rather than fetched. `referenceBase64` and `beautyBase64` are optional for a networked viewer, but required for a no-network artifact. The embedded GLB is also wired to the model download action, so that action remains local in the offline page. The embed seam is also exposed as `window.RainGalleryViewer.embedContract` for packagers and `window.RainGalleryViewer.reload()` for a manual retry.

For the generated one-file artifact, inject the object in a normal `<script>` and then inline the built CSS and JavaScript from `dist/`; do not leave the hashed `dist/assets/*.js` or `dist/assets/*.css` references external. Keep the embed script before the inline module. The ordinary web build still serves the original `.blend`, `.glb`, render, Godot ZIP, and offline HTML through the download menu.

## Verification

```bash
npm test
npm run build
```
