# Contributing to nanoRag

We welcome contributions from the community! This document explains how to contribute to this project.

## Code of Conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## Contributor License Agreement (CLA)

All external contributors must sign the [Salesforce Contributor License Agreement](https://cla.salesforce.com/sign-cla). You only need to sign it once — it covers all Salesforce open-source projects.

If you haven't signed the CLA, a bot will comment on your pull request with instructions.

## How to Contribute

### Reporting Issues

- Use GitHub Issues to report bugs or request features.
- Search existing issues first to avoid duplicates.
- Include reproduction steps, expected behavior, and environment details.

### Pull Requests

1. Fork the repository and create your branch from `main`.
2. If you've added code, add tests where applicable.
3. Ensure your code passes linting and type checks.
4. Write a clear PR description explaining what changed and why.

### Development Setup

```bash
# Clone your fork
git clone https://github.com/<your-username>/nanorag.git
cd nanorag

# Install Python package in development mode
pip install -e .

# Build the SF CLI plugin
cd plugins/sf-nanorag
npm install
npm run build

# Link for local testing
sf plugins link plugins/sf-nanorag
```

### Code Style

- Python: Follow PEP 8. Use type hints for function signatures.
- TypeScript: Follow the existing ESM style. Use strict mode.
- Keep changes focused — one logical change per PR.

### Testing

```bash
# Test Python package imports
python -c "import nanorag; print('OK')"

# Test the CLI bridge
echo '{"command": "library_list", "args": {}}' | \
  SF_ACCESS_TOKEN=... SF_INSTANCE_URL=... python -m nanorag.cli_runner

# Build TypeScript
cd plugins/sf-nanorag && npm run build
```

## Questions?

Open a GitHub Discussion or Issue if you're unsure about anything.
