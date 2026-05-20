// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../../helpers/python-runner.js';

export default class NanoragFileDelete extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Delete files from a nanoRag library.';
  public static readonly examples = [
    '$ sf nanorag file delete --target-org myOrg --library-name my_lib --filename old_doc.pdf',
    '$ sf nanorag file delete --target-org myOrg --library-name my_lib --all',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library.' }),
    filename: Flags.string({ summary: 'Specific filename to delete.' }),
    all: Flags.boolean({ summary: 'Delete all files in the library.', default: false }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragFileDelete);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    if (!flags.filename && !flags.all) {
      this.error('Specify --filename or --all.');
    }

    this.spinner.start('Deleting files');

    const result = await runPython(
      'file_delete',
      { library_name: flags['library-name'], filename: flags.filename, all: flags.all },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess('Files deleted.');
    return result.result;
  }
}
