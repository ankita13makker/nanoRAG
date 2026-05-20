// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../../helpers/python-runner.js';

export default class NanoragLibraryDelete extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Delete a nanoRag library and all its files from the org.';
  public static readonly examples = ['$ sf nanorag library delete --target-org myOrg --library-name my_lib'];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library to delete.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragLibraryDelete);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start(`Deleting library "${flags['library-name']}"`);

    const result = await runPython(
      'library_delete',
      { library_name: flags['library-name'] },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess(`Library "${flags['library-name']}" deleted.`);
    return result.result;
  }
}
