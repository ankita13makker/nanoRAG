#!/usr/bin/env node
/**
 * Postinstall hook: creates a Python virtual environment in the plugin directory
 * and installs dependencies from the repo's requirements.txt.
 */

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = resolve(fileURLToPath(import.meta.url), '..');
const PLUGIN_ROOT = resolve(__dirname, '..');
const REPO_ROOT = resolve(PLUGIN_ROOT, '..', '..');
const VENV_DIR = join(PLUGIN_ROOT, '.venv');
const REQUIREMENTS = join(REPO_ROOT, 'requirements.txt');

function findPython() {
  const candidates = process.platform === 'win32'
    ? ['python3', 'python']
    : ['python3', 'python'];

  for (const cmd of candidates) {
    try {
      const version = execFileSync(cmd, ['--version'], { encoding: 'utf-8' }).trim();
      const match = version.match(/Python (\d+)\.(\d+)/);
      if (match && parseInt(match[1]) >= 3 && parseInt(match[2]) >= 10) {
        return cmd;
      }
    } catch {
      continue;
    }
  }
  return null;
}

if (existsSync(join(VENV_DIR, process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'))) {
  console.log('[nanorag] Python venv already exists, skipping setup.');
  process.exit(0);
}

const python = findPython();
if (!python) {
  console.error('[nanorag] ERROR: Python 3.10+ not found. Install Python from https://python.org');
  process.exit(1);
}

console.log(`[nanorag] Creating virtual environment with ${python}...`);
execFileSync(python, ['-m', 'venv', VENV_DIR], { stdio: 'inherit' });

const pip = process.platform === 'win32'
  ? join(VENV_DIR, 'Scripts', 'pip.exe')
  : join(VENV_DIR, 'bin', 'pip');

console.log('[nanorag] Installing Python dependencies...');
execFileSync(pip, ['install', '--quiet', '-r', REQUIREMENTS], { stdio: 'inherit' });

console.log('[nanorag] Setup complete.');
console.log('');
console.log('[nanorag] To enable Claude Code integration, run:');
console.log('         sf nanorag skill install');
