# nanoRag

Lightweight BM25 knowledge retrieval for Salesforce Agentforce agents.

Give any **NextGen Agentforce agent** the ability to search and answer from your documents — PDFs, DOCX, PPTX, XLSX, TXT, and 30+ other formats. No external vector database. No API keys. Everything runs locally and stores in your Salesforce org.

> **Agent compatibility:** nanoRAG attaches to **NextGen (AgentScript) agents** only — the new Agentforce agent format authored via the NextGen Authoring API. Legacy `BotDefinition` agents are detected and a helpful upgrade message is shown.

## How It Works

```
Your Documents → Extract Text → Build BM25 Index → Upload to Org → Agent Queries at Runtime
     (local)       (local)          (local)         (SF Files)        (Apex)
```

1. **Extract** — Pulls text from PDFs, Word docs, spreadsheets, etc.
2. **Index** — Builds a BM25 keyword index (no embeddings, no GPU)
3. **Store** — Uploads everything to Salesforce Files (ContentVersion)
4. **Query** — Deployed Apex class scores queries against the index at runtime
5. **Wire into the agent** — Auto-updates the agent's AgentScript with a dedicated subagent and search action via the NextGen Authoring API. Topic name, routing description, and instructions are LLM-generated. Files are shared with the agent's runtime user and the `NanoRag_User` permission set is assigned automatically. **No manual `.agent` file editing required.**

## Quick Start

### Prerequisites

- [Salesforce CLI](https://developer.salesforce.com/tools/salesforcecli) (`sf`) installed
- Python 3.10+ on PATH
- A Salesforce org with Agentforce enabled
- A **NextGen (AgentScript) agent** in the org — created via Setup → Agents (the new authoring experience). Legacy `BotDefinition` agents are not supported.
- An authenticated org: `sf org login web --alias myOrg`

### Install

```bash
# Clone the repo
git clone https://github.com/ankita13makker/nanoRAG.git
cd nanoRAG

# Link the SF CLI plugin (creates venv + installs Python deps automatically)
sf plugins link plugins/sf-nanorag
```

### Three Commands to Knowledge-Enabled Agent

```bash
# 1. Deploy Apex runtime classes (one-time per org)
sf nanorag install --target-org myOrg --json

# 2. Build a library from your documents
sf nanorag build --target-org myOrg --library-name product_docs \
  --files ./docs/guide.pdf ./docs/faq.docx --json

# 3. Attach to your agent
sf nanorag attach --target-org myOrg --library-name product_docs \
  --agent-developer-name My_Service_Agent --json
```

Done. Your agent can now answer questions from those documents.

### What `attach` actually does

`sf nanorag attach` doesn't just associate metadata — it **updates the agent's AgentScript** so the runtime can route knowledge questions to the BM25 search. Each attach call:

1. **Creates a new draft version** of the agent via the NextGen Authoring API
2. **Injects a dedicated subagent block** named `nanorag_<topic>` into the AgentScript with LLM-generated routing instructions
3. **Adds an action** (`search_<topic>`) that calls the deployed `NanoRagQueryService` Apex class with `userQuery` and `libraryName` inputs
4. **Wires a router transition** so the orchestrator escalates relevant queries to the new subagent
5. **Shares all library files** (`raw/`, `extracted/`, `index/bm25.json`, `manifest.json`, `memory.md`) with the agent's `default_agent_user`
6. **Assigns the `NanoRag_User` permission set** to that user so the deployed Apex can read the index at query time

After attach, you can test in the agent preview — your agent will route questions like "what's covered in the cancer policy?" to the new subagent, which calls Apex, which scores the query against the BM25 index and returns the top-K matching documents' text.

`sf nanorag detach` cleanly removes the topic block, action, and router transition from the AgentScript while leaving the library files intact for re-attach.

## All Commands

| Command | Description |
|---------|-------------|
| `sf nanorag install` | Deploy Apex classes + permission set (one-time) |
| `sf nanorag build` | Extract, index, and upload a document library |
| `sf nanorag attach` | Wire a library into an agent's AgentScript |
| `sf nanorag detach` | Remove a library from an agent |
| `sf nanorag search` | Test BM25 search against a library |
| `sf nanorag library list` | List all libraries in the org |
| `sf nanorag library delete` | Delete a library (auto-detaches from agents) |
| `sf nanorag file list` | List files in a library |
| `sf nanorag file add` | Add files (rebuilds index, updates attached agents) |
| `sf nanorag file delete` | Remove files (rebuilds index, updates attached agents) |
| `sf nanorag skill install` | Install Claude Code skill for AI-assisted workflows |

All commands support `--json` for structured output.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your Machine (local)                                    │
│                                                          │
│  sf nanorag build                                        │
│    ├── Extract text (PyMuPDF, python-docx, openpyxl...) │
│    ├── Chunk + tokenize + stem                           │
│    ├── Build BM25 index (tf/df/avgdl per document)       │
│    └── Upload to org (ContentVersion REST API)           │
│                                                          │
│  sf nanorag attach                                       │
│    ├── Generate topic metadata (LLM Gateway or local)    │
│    ├── Inject topic into AgentScript (NextGen API)       │
│    └── Share files with agent runtime user               │
└─────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Salesforce Org                                          │
│                                                          │
│  ContentVersion (Salesforce Files)                        │
│    └── nanorag/{library}/                                │
│        ├── raw/{filename}        — original files        │
│        ├── extracted/{file}.txt  — extracted text         │
│        ├── bm25.json             — BM25 index            │
│        ├── memory.md             — file summaries         │
│        └── manifest.json         — library metadata       │
│                                                          │
│  Apex Classes (deployed by `sf nanorag install`)          │
│    ├── NanoRagTokenizer          — stemming + bigrams    │
│    ├── NanoRagBM25Scorer         — BM25 scoring engine   │
│    ├── NanoRagQueryService       — @InvocableMethod      │
│    └── NanoRagQueryServiceTest   — test coverage         │
│                                                          │
│  Agent (AgentScript)                                      │
│    └── subagent nanorag_{topic}: — injected topic block  │
│        └── actions: search_{topic} → NanoRagQueryService │
└─────────────────────────────────────────────────────────┘
```

## Supported File Formats

| Category | Extensions |
|----------|-----------|
| Documents | `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.rtf`, `.epub`, `.odt` |
| Text | `.txt`, `.md`, `.csv`, `.json`, `.yaml`, `.yml`, `.html` |
| Code | `.py`, `.java`, `.js`, `.ts`, `.sql`, `.apex`, `.cls`, `.go`, `.rb`, `.swift`, `.kt` |

## Claude Code Integration

Install the skill for AI-assisted library management:

```bash
sf nanorag skill install
```

This copies `SKILL.md` to `~/.claude/skills/nanorag/`. In new Claude Code sessions, you can say:

> "Give my agent access to these PDFs as a knowledge library"

And Claude Code will orchestrate the install → build → attach flow automatically.

## How BM25 Scoring Works

nanoRag uses BM25 (Best Matching 25), a probabilistic ranking function:

1. **Tokenization** — Text is lowercased, split into words, stopwords removed
2. **Stemming** — Rule-based suffix stripping (e.g., "running" → "run")
3. **Bigrams** — Adjacent stems are paired (e.g., "machine_learn")
4. **TF-IDF scoring** — Term frequency weighted by inverse document frequency
5. **Length normalization** — Shorter documents aren't penalized

The same tokenizer runs in Python (build time) and Apex (query time), ensuring scoring parity.

## Limits

| Limit | Value |
|-------|-------|
| Max file size | 10 MB |
| Max files per library | 25 |
| Per-document char cap (runtime) | 100,000 |
| Top-K results returned | 2 (configurable in Apex) |

## Development

```bash
# Install Python package in development mode
pip install -e .

# Test the Python CLI bridge directly
echo '{"command": "library_list", "args": {}}' | \
  SF_ACCESS_TOKEN=... SF_INSTANCE_URL=... python -m nanorag.cli_runner

# Build the TypeScript plugin
cd plugins/sf-nanorag && npm install && npm run build

# Link for local testing
sf plugins link plugins/sf-nanorag
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache-2.0. See [LICENSE](LICENSE).
