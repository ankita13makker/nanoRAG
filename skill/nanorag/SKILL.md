# nanoRag Knowledge Library Skill

## When to Use

Use this skill when the user wants to:
- Give an Agentforce agent knowledge from documents (PDFs, DOCX, PPTX, XLSX, RTF, TXT)
- Create, manage, or query nanoRag knowledge libraries
- Attach or detach document knowledge from agents
- List, add, or delete files in a knowledge library
- Deploy nanoRag runtime classes to an org

**Trigger phrases**: "knowledge", "documents", "library", "nanorag", "give the agent access to files", "upload documents", "attach knowledge", "RAG", "retrieval"

## Prerequisites

1. **SF CLI installed** with a target org configured (`sf config set target-org <alias>`)
2. **Python 3.10+** on PATH (the plugin auto-creates a venv on first use)
3. **Plugin linked**: `sf plugins link plugins/sf-nanorag` (from the `agentforce-nanoRAG` repo root)

## Complete Workflow

The typical end-to-end flow is:

```
sf nanorag install       → Deploy Apex classes (one-time per org)
sf nanorag build         → Extract text, build BM25 index, upload to org
sf nanorag attach        → Generate topic metadata (LLM) + wire into agent
```

That's it. Three commands. Topic metadata is generated automatically during attach.

### IMPORTANT: Always run install first

Before build or attach, ALWAYS run `sf nanorag install --json` unless you have confirmed in a prior turn that the Apex classes are already deployed. If you skip this and the classes don't exist, the agent will get a `MISSING_RECORD` error at runtime. Install is idempotent — safe to run multiple times.

### Gathering required information

If the user doesn't provide these, ASK before proceeding:
1. **File paths** — which documents to give the agent access to
2. **Agent developer name** — the API name of the **NextGen agent** (Agentforce). Find it via the NextGen Authoring API:
   ```bash
   curl -s -H "Authorization: Bearer $TOKEN" \
     "$INSTANCE_URL/services/data/v66.0/connect/nextgen-authoring/projects"
   ```
   Look for the `apiName` field (project IDs are prefixed `1bY`). Or ask the user to check Setup > Agents in the org.

   **Do NOT** query `BotDefinition` via SOQL — that returns only legacy Bot agents, not NextGen authoring projects. nanoRag only attaches to NextGen agents.
3. **Target org** — confirm `sf config get target-org --json` is set; if not, ask user to run `sf config set target-org <alias>`

### When to Override Topic Metadata

The attach command generates topic metadata automatically via the Salesforce LLM Gateway. Override manually ONLY if:
- The LLM-generated metadata is poor quality (wrong domain, too vague)
- The org's NamedUser bootstrap endpoint is unavailable
- You want very specific routing instructions

**To override**, after build but before attach:

1. Read the generated `memory.md` from the local output directory:
   `nanorag/{library_name}/memory.md`

2. Generate three things:

   - **topic_name**: A short `snake_case` identifier (max 40 chars).
     Examples: `vehicle_diagnostics`, `hr_policies`, `ipl_2025_cricket`

   - **description**: A one-line routing description (max 200 chars) starting with
     "Route here for questions about ..." covering key domains this library handles.

   - **instructions**: 3-5 lines telling the subagent when to search, how to cite sources,
     and to never answer from general knowledge.

3. Write the metadata into the local manifest.json:

```python
import json
from pathlib import Path

manifest_path = Path("nanorag/{library_name}/manifest.json")
manifest = json.loads(manifest_path.read_text())
manifest["topic_name"] = "<your generated topic_name>"
manifest["topic_description"] = "<your generated description>"
manifest["topic_instructions"] = ["<line 1>", "<line 2>", "<line 3>"]
manifest["topic_source"] = "claude_code"
manifest_path.write_text(json.dumps(manifest, indent=2))
```

4. Re-run build to upload the updated manifest, then attach:

```bash
sf nanorag build --target-org myOrg --library-name {library_name} --files <same files> --json
sf nanorag attach --target-org myOrg --library-name {library_name} --agent-developer-name My_Agent --json
```

When `topic_source` is `"claude_code"`, attach uses the manifest metadata directly instead of regenerating.

## Commands

### 1. Install Foundation (one-time per org)

Deploys 4 Apex classes (NanoRag_Tokenizer, NanoRag_BM25Scorer, NanoRag_QueryService, NanoRag_QueryServiceTest) and the NanoRag_User permission set.

```bash
sf nanorag install --target-org myOrg --json
```

**When to run**: Before first library build. Skip if classes already exist.

### 2. Build a Library

Extracts text from local files, builds a BM25 index, and uploads everything (raw files + extracted text + index + manifest) to the org as Salesforce Files (ContentVersion).

```bash
sf nanorag build --target-org myOrg --library-name product_docs --files ./docs/guide.pdf ./docs/faq.docx --json
```

**Flags:**
- `--library-name` (required): Identifier for the library (lowercase, underscores)
- `--files` (required, multiple): Local paths to documents

**Supported formats**: PDF, DOCX, PPTX, XLSX, RTF, TXT, CSV, MD, HTML, JSON, YAML

### 3. Attach Library to Agent

Wires a built library into an Agentforce agent so the agent can query it at runtime.

```bash
sf nanorag attach --target-org myOrg --library-name product_docs --agent-developer-name My_Service_Agent --json
```

**What it does**:
1. Loads manifest from org to get library metadata
2. Generates topic metadata via LLM Gateway (or uses existing if `topic_source` is `"claude_code"` or `"llm"`)
3. Finds the agent project via NextGen Authoring API
4. Injects a nanoRag query topic into the agent's AgentScript
5. Shares library files with the agent's runtime user (ContentDocumentLink)
6. Assigns NanoRag_User permission set to agent runtime user
7. Creates a new draft version with the updated AgentScript

**Agent requirements**: Must be a NextGen (AgentScript) agent. Legacy BotDefinition agents are detected and a helpful upgrade message is shown.

### 4. Detach Library from Agent

Removes a library's topic from an agent without deleting the library files.

```bash
sf nanorag detach --target-org myOrg --library-name product_docs --agent-developer-name My_Service_Agent --json
```

### 5. List Libraries

Shows all nanoRag libraries stored in the org.

```bash
sf nanorag library list --target-org myOrg --json
```

**Output**: Library name, file count, whether BM25 index exists.

### 6. Delete a Library

Removes all files (raw, extracted, index, manifest) for a library from the org. Automatically detaches from any agents the library is attached to.

```bash
sf nanorag library delete --target-org myOrg --library-name old_lib --json
```

### 7. List Files in a Library

```bash
sf nanorag file list --target-org myOrg --library-name product_docs --json
```

### 8. Add Files to a Library

Adds new files, rebuilds the BM25 index from all files (existing + new), regenerates topic metadata, and updates any attached agents.

```bash
sf nanorag file add --target-org myOrg --library-name product_docs --files ./new_doc.pdf --json
```

### 9. Delete Files from a Library

Removes files, rebuilds BM25 index from remaining files, regenerates topic metadata, and updates any attached agents.

```bash
# Delete a specific file
sf nanorag file delete --target-org myOrg --library-name product_docs --filename old_doc.pdf --json

# Delete all files (keeps library shell)
sf nanorag file delete --target-org myOrg --library-name product_docs --all --json
```

## JSON Output Format

All commands support `--json` and return structured output:

```json
{
  "status": 0,
  "result": {
    "library_name": "product_docs",
    "files_indexed": 3
  }
}
```

On error:
```json
{
  "status": 1,
  "name": "PythonError",
  "message": "Description of what went wrong"
}
```

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| `Python 3.10+ not found` | No compatible Python on PATH | Install Python 3.10+ from python.org |
| `PythonSpawnError` | venv or dependencies broken | Delete `plugins/sf-nanorag/.venv/` and re-run |
| `missing_arg` | Required flag not provided | Check command flags above |
| `INVALID_SESSION_ID` | Token expired | Re-authenticate: `sf org login web --alias myOrg` |
| `deploy_failed` | Apex deploy issue | Check org for existing classes with same name |
| `agent_not_found` | Agent doesn't exist as NextGen | Verify agent developer name in Setup > Agents |
| `legacy Bot agent` | Agent is BotDefinition | Upgrade to AgentScript in Setup > Agents |

## Architecture Notes

- **No external server**: Everything runs locally. Python does extraction + indexing; org stores files.
- **Storage**: Libraries live in Salesforce Files (ContentVersion) with title convention `nanorag/{lib}/...`
- **Runtime**: Agent queries the BM25 index via the deployed NanoRag_QueryService Apex class
- **Auth**: Uses the SF CLI's existing org auth (`--target-org`) — no separate credentials needed
- **Venv**: Plugin manages its own `.venv/` directory, isolated from system Python
- **Topic metadata**: Generated via orgJWT → global LLM Gateway (no AiApplication needed). Falls back to deterministic extraction if LLM unavailable.
- **Mutations are cascading**: file add/delete rebuilds the index and updates all attached agents automatically.
