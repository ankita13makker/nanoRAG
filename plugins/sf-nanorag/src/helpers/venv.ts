// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const PLUGIN_ROOT = resolve(fileURLToPath(import.meta.url), '..', '..', '..');
const REPO_ROOT = resolve(PLUGIN_ROOT, '..', '..');
const VENV_DIR = join(PLUGIN_ROOT, '.venv');
const REQUIREMENTS = join(REPO_ROOT, 'requirements.txt');

function isWindows(): boolean {
  return process.platform === 'win32';
}

export function getPythonPath(): string {
  if (isWindows()) {
    return join(VENV_DIR, 'Scripts', 'python.exe');
  }
  return join(VENV_DIR, 'bin', 'python');
}

export function getRepoRoot(): string {
  return REPO_ROOT;
}

export function ensureVenv(): void {
  if (existsSync(getPythonPath())) {
    return;
  }

  const python = findSystemPython();
  if (!python) {
    throw new Error(
      'Python 3.10+ not found. Install Python from https://python.org and ensure python3 is on PATH.',
    );
  }

  execFileSync(python, ['-m', 'venv', VENV_DIR], { stdio: 'inherit' });

  const pip = isWindows()
    ? join(VENV_DIR, 'Scripts', 'pip.exe')
    : join(VENV_DIR, 'bin', 'pip');

  execFileSync(pip, ['install', '--quiet', '-r', REQUIREMENTS], { stdio: 'inherit' });
}

function findSystemPython(): string | null {
  const candidates = isWindows()
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
