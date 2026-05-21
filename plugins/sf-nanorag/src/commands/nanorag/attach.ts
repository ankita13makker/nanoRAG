// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../helpers/python-runner.js';

export default class NanoragAttach extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Attach a nanoRag library to an Agentforce agent.';
  public static readonly examples = [
    '$ sf nanorag attach --target-org myOrg --library-name my_lib --agent-developer-name My_Agent',
  ];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
    'library-name': Flags.string({ required: true, summary: 'Name of the library to attach.' }),
    'agent-developer-name': Flags.string({ required: true, summary: 'Developer name of the target agent.' }),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragAttach);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start(`Attaching "${flags['library-name']}" to agent "${flags['agent-developer-name']}"`);

    const result = await runPython(
      'attach',
      { library_name: flags['library-name'], agent_developer_name: flags['agent-developer-name'] },
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();

    const r = result.result as Record<string, unknown>;
    const status = r.status as string;
    if (status === 'attached_with_share_failure') {
      this.warn(
        (r.warning as string) ||
          'AgentScript patched, but file sharing did not fully succeed. ' +
            'The agent will return empty search results at runtime.',
      );
      const failures = (r.share_failures as Array<{ content_document_id: string; error: string }>) ?? [];
      for (const f of failures) {
        this.log(`  - ${f.content_document_id}: ${f.error}`);
      }
      for (const k of ['fatal_error', 'user_resolution_error', 'permset_error']) {
        if (r[k]) this.log(`  ${k}: ${r[k] as string}`);
      }
      this.error('Attach completed with errors. See warnings above.', { exit: 1 });
    }

    this.logSuccess('Library attached to agent.');
    return result.result;
  }
}
