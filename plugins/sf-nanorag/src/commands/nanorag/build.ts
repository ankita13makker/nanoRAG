// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../helpers/python-runner.js';

export default class NanoragBuild extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Build a nanoRag library from local files and upload to the org.';
  public static readonly examples = [
    '$ sf nanorag build --target-org myOrg --library-name my_lib --files ./docs/guide.pdf ./docs/faq.docx',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name for the knowledge library.' }),
    files: Flags.string({ multiple: true, required: true, summary: 'Local file paths to include in the library.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragBuild);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start(`Building library "${flags['library-name']}"`);

    const result = await runPython(
      'build',
      { library_name: flags['library-name'], files: flags.files },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess(`Library "${flags['library-name']}" built and uploaded.`);
    return result.result;
  }
}
