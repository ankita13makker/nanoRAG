// Copyright (c) 2026, Salesforce, Inc.
// SPDX-License-Identifier: Apache-2.0

import { SfCommand, Flags } from '@salesforce/sf-plugins-core';
import { runPython, PythonResult } from '../../helpers/python-runner.js';

export default class NanoragInstall extends SfCommand<PythonResult['result']> {
  public static readonly summary = 'Deploy nanoRag Apex classes and permission set to the target org.';
  public static readonly examples = ['$ sf nanorag install --target-org myOrg'];

  public static readonly flags = {
    'target-org': Flags.requiredOrg(),
  };

  public async run(): Promise<PythonResult['result']> {
    const { flags } = await this.parse(NanoragInstall);
    const org = flags['target-org'];
    await org.refreshAuth();
    const conn = org.getConnection();

    this.spinner.start('Deploying nanoRag foundation to org');

    const result = await runPython(
      'install',
      {},
      { SF_ACCESS_TOKEN: conn.accessToken!, SF_INSTANCE_URL: conn.instanceUrl },
      (line) => this.log(line),
    );

    this.spinner.stop();
    this.logSuccess('nanoRag foundation deployed successfully.');
    return result.result;
  }
}
