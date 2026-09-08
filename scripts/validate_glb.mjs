import { readFile, writeFile } from 'node:fs/promises';
import validator from 'gltf-validator';

const bytes = new Uint8Array(await readFile(new URL('../public/assets/rain-gallery.glb', import.meta.url)));
const report = await validator.validateBytes(bytes, { uri: 'rain-gallery.glb', maxIssues: 100 });
await writeFile(new URL('../docs/gltf-validation.json', import.meta.url), `${JSON.stringify(report, null, 2)}\n`);
console.log(JSON.stringify({ validator: report.validatorVersion, bytes: bytes.length, errors: report.issues.numErrors, warnings: report.issues.numWarnings, infos: report.issues.numInfos }, null, 2));
if (report.issues.numErrors) {
  console.error(JSON.stringify(report.issues.messages, null, 2));
  process.exitCode = 1;
}
