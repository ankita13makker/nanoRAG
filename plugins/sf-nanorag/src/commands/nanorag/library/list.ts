// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../../helpers/python-runner.js';

export default class NanoragLibraryList extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'List all nanoRag libraries in the org.';
  public static readonly examples = ['$ sf nanorag library list --target-org myOrg'];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragLibraryList);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    const result = await runPython(
      'library_list',
      {},
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    const libraries = (result.result as { libraries: Array<{ name: string; file_count: number; has_index: boolean }> }).libraries;
    if (libraries.length === 0) {
      this.log('No libraries found in this org.');
    } else {
      this.table({ data: libraries, columns: ['name', 'file_count', 'has_index'] });
    }

    return result.result;
  }
}
