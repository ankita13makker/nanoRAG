// Copyright (c) 2024, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../helpers/python-runner.js';

export default class NanoragDetach extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Detach a nanoRag library from an Agentforce agent.';
  public static readonly examples = [
    '$ sf nanorag detach --target-org myOrg --library-name my_lib --agent-developer-name My_Agent',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library to detach.' }),
    'agent-developer-name': Flags.string({ required: true, summary: 'Developer name of the target agent.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragDetach);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start(`Detaching "${flags['library-name']}" from agent "${flags['agent-developer-name']}"`);

    const result = await runPython(
      'detach',
      { library_name: flags['library-name'], agent_developer_name: flags['agent-developer-name'] },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess('Library detached from agent.');
    return result.result;
  }
}
