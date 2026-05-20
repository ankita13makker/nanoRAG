#!/usr/bin/env node
/**
 * Generates the oclif.manifest.json file required for SF CLI plugin discovery.
 * Called by `npm run build`.
 */

import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = resolve(fileURLToPath(import.meta.url), '..');
const pluginRoot = resolve(__dirname, '..');

try {
  execFileSync('npx', ['oclif', 'manifest'], { cwd: pluginRoot, stdio: 'inherit' });
} catch {
  console.warn('[nanorag] Warning: oclif manifest generation failed. Run `npx oclif manifest` manually.');
}
