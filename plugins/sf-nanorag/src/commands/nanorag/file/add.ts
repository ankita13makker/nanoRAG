// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../../helpers/python-runner.js';

export default class NanoragFileAdd extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Add files to an existing nanoRag library (rebuilds index).';
  public static readonly examples = [
    '$ sf nanorag file add --target-org myOrg --library-name my_lib --files ./new_doc.pdf ./another.docx',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library.' }),
    files: Flags.string({ multiple: true, required: true, summary: 'Local file paths to add.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragFileAdd);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start(`Adding files to "${flags['library-name']}"`);

    const result = await runPython(
      'file_add',
      { library_name: flags['library-name'], files: flags.files },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess('Files added and index rebuilt.');
    return result.result;
  }
}
