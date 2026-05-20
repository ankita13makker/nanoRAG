// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { spawn } from 'node:child_process';
import { SfError } from '@salesforce/core';
import { ensureVenv, getPythonPath, getRepoRoot } from './venv.js';

export interface PythonResult {
  status: string;
  result?: unknown;
  error?: string;
  message?: string;
}

export interface PythonEnv {
  SF_ACCESS_TOKEN: string;
  SF_INSTANCE_URL: string;
}

export async function runPython(
  command: string,
  args: Record<string, unknown>,
  env: PythonEnv,
  onStderr?: (line: string) => void,
): Promise<PythonResult> {
  ensureVenv();

  const pythonPath = getPythonPath();
  const input = JSON.stringify({ command, args });

  return new Promise((resolve, reject) => {
    const proc = spawn(pythonPath, ['-m', 'nanorag.cli_runner'], {
      cwd: getRepoRoot(),
      env: {
        ...process.env,
        SF_ACCESS_TOKEN: env.SF_ACCESS_TOKEN,
        SF_INSTANCE_URL: env.SF_INSTANCE_URL,
        PYTHONUNBUFFERED: '1',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (data: Buffer) => {
      stdout += data.toString();
    });

    proc.stderr.on('data', (data: Buffer) => {
      const text = data.toString();
      stderr += text;
      if (onStderr) {
        for (const line of text.split('\n').filter(Boolean)) {
          onStderr(line);
        }
      }
    });

    proc.on('error', (err: Error) => {
      reject(new SfError(`Failed to spawn Python: ${err.message}`, 'PythonSpawnError'));
    });

    proc.on('close', (code: number | null) => {
      if (!stdout.trim()) {
        reject(
          new SfError(
            `Python process exited with code ${code} and no output.\nStderr: ${stderr}`,
            'PythonNoOutput',
          ),
        );
        return;
      }

      try {
        const result = JSON.parse(stdout) as PythonResult;
        if (result.status === 'error') {
          reject(new SfError(result.message ?? 'Unknown Python error', result.error ?? 'PythonError'));
          return;
        }
        resolve(result);
      } catch {
        reject(
          new SfError(
            `Failed to parse Python output as JSON.\nOutput: ${stdout}\nStderr: ${stderr}`,
            'PythonJsonParseError',
          ),
        );
      }
    });

    proc.stdin.write(input);
    proc.stdin.end();
  });
}
