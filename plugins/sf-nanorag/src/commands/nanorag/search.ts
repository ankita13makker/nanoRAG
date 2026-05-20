// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../helpers/python-runner.js';

export default class NanoragSearch extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Search a library with a query and return BM25-ranked file hits.';
  public static readonly examples = [
    '$ sf nanorag search --target-org myOrg --library-name product_docs --query "how to configure SSO"',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Library to search.' }),
    query: Flags.string({ required: true, char: 'q', summary: 'Search query.' }),
    'top-k': Flags.integer({ default: 3, summary: 'Number of top results to return.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragSearch);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    const result = await runPython(
      'search',
      {
        library_name: flags['library-name'],
        query: flags.query,
        top_k: flags['top-k'],
      },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    return result.result;
  }
}
