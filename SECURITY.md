# Security Policy

## Reporting a Vulnerability

The Salesforce Product Security team takes security seriously. We appreciate your efforts to responsibly disclose your findings.

**Please do NOT report security vulnerabilities through public GitHub issues.**

Instead, report them via the [Salesforce Responsible Disclosure Program](https://www.salesforce.com/company/responsible-disclosure/). You can also email security@salesforce.com.

Please include:

- Description of the vulnerability
- Steps to reproduce
- Impact assessment
- Any suggested mitigations

You should receive a response within 48 hours. If you don't hear back, follow up via email to ensure we received your original message.

## Supported Versions

| Version | Supported |
|---------|-----------|
| Latest  | Yes       |

## Security Best Practices for Users

- Never commit `.env` files or credentials to version control
- Use `sf org login web` for authentication (tokens managed by SF CLI)
- Keep dependencies up to date (`pip install --upgrade` / `npm update`)
- Review the [Salesforce Security Guide](https://developer.salesforce.com/docs/atlas.en-us.securityImplGuide.meta/securityImplGuide/) for org-level security
