// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand } from '@salesforce/sf-plugins-core';
import { existsSync, mkdirSync, copyFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

export default class NanoragSkillInstall extends SfCommand<{ installed: boolean; path: string }> {
  public static readonly summary = 'Install the nanoRag Claude Code skill for AI-assisted library management.';
  public static readonly description =
    'Copies the nanoRag skill file to ~/.claude/skills/nanorag/ so Claude Code can orchestrate library workflows automatically.';
  public static readonly examples = ['$ sf nanorag skill install'];

  public async run(): Promise<{ installed: boolean; path: string }> {
    const pluginRoot = resolve(fileURLToPath(import.meta.url), '..', '..', '..', '..', '..');
    const repoRoot = resolve(pluginRoot, '..', '..');
    const skillSource = join(repoRoot, 'skill', 'nanorag', 'SKILL.md');

    if (!existsSync(skillSource)) {
      this.error('SKILL.md not found. Ensure you are running from the nanorag repo.');
    }

    const targetDir = join(homedir(), '.claude', 'skills', 'nanorag');
    const targetPath = join(targetDir, 'SKILL.md');

    mkdirSync(targetDir, { recursive: true });
    copyFileSync(skillSource, targetPath);

    this.logSuccess(`Claude Code skill installed to ${targetPath}`);
    this.log('Claude Code will now recognize nanoRag commands in new sessions.');

    return { installed: true, path: targetPath };
  }
}
