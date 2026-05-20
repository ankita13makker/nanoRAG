// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../../helpers/python-runner.js';

export default class NanoragFileList extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'List files in a nanoRag library.';
  public static readonly examples = ['$ sf nanorag file list --target-org myOrg --library-name my_lib'];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragFileList);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    const result = await runPython(
      'file_list',
      { library_name: flags['library-name'] },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    const files = (result.result as { files: Array<{ filename: string; size_bytes: number }> }).files;
    if (files.length === 0) {
      this.log(`No files in library "${flags['library-name']}".`);
    } else {
      this.table({ data: files, columns: ['filename', 'size_bytes'] });
    }

    return result.result;
  }
}
