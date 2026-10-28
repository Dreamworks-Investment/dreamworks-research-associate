# Deep Value MCP Wrapper MVP

This repo shows **what a wrapper actually looks like** for your local `.skill` package.

It turns a dry skill archive into a small runnable product:

- **Frontend UI**: local chat page at `http://localhost:8000`
- **Backend orchestrator**: FastAPI app that loads the skill, calls an LLM, and generates reports
- **Outbound MCP server**: exposes selected research/report functions to cloud clients
- **Inbound provider adapters**: thin connectors for third-party data providers such as "Alpha Pai"
- **LLM adapter**: server-side OpenAI API integration
- **Export pipeline**: markdown report download now, with PDF/DOCX generation hooks included

## Mental model

Your `.skill` file is **not** an app. It is only one layer:

1. **Skill content layer**  
   Your `SKILL.md` + `references/*.md` define behavior, structure, formatting rules, and report template logic.

2. **Agent runtime layer**  
   Loads the skill, decides which tools to call, manages memory/state, calls the LLM, and builds a final report.

3. **Integration layer**  
   MCP server(s), provider adapters, auth, API keys, cloud deployment, logs, and rate limits.

4. **Experience layer**  
   Chat UI, report download, user history, etc.

## Recommended MVP path

Do **not** start with a huge multi-agent cloud platform.

Start with this 3-step path:

### Step 1 — local MVP
Run:
- FastAPI backend
- static HTML chat UI
- local skill loader
- OpenAI adapter
- Alpha Pai REST adapter stub
- markdown report export

### Step 2 — outbound MCP
Expose your core abilities as MCP tools:
- `run_deep_value_report`
- `preview_report_outline`
- `export_report`

This lets Bailian / other MCP-capable clients call your service.

### Step 3 — cloud plug-in
Choose one of these depending on what you already have:
- If you have **MCP code** already: deploy as a custom MCP service with script deployment.
- If you have a **plain REST API** already: import through Alibaba Cloud AI Gateway and wrap it into MCP.

## Why one backend first

For an MVP, keep **one backend** as the orchestrator:
- the browser talks only to your backend
- your backend holds API keys
- your backend calls OpenAI + Alpha Pai
- your backend optionally exposes MCP to the cloud

Do **not** put OpenAI or Alpha Pai keys in the browser.

## Project layout

```text
deep_value_mcp_wrapper_mvp/
├── README.md
├── .env.example
├── requirements.txt
├── run_local.sh
├── backend/
│   ├── app.py
│   ├── config.py
│   ├── skill_runtime.py
│   ├── report_export.py
│   └── providers/
│       ├── openai_provider.py
│       └── alphapai_provider.py
├── mcp_server/
│   └── server.py
├── frontend/
│   └── index.html
└── reports/
```

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

# point SKILL_ARCHIVE to your real local file
# for example:
# SKILL_ARCHIVE=/absolute/path/to/modern-deep-value-report-team-V2.skill

uvicorn backend.app:app --reload --port 8000
```

Open:
- UI: `http://localhost:8000`
- Health: `http://localhost:8000/healthz`

## Environment variables

See `.env.example`.

## Report formats

Implemented:
- `.md`

Stubbed with code hooks:
- `.pdf`
- `.docx`

## How to connect to Alibaba Cloud Bailian later

### Option A: script deployment
Best when you have MCP server code already.
Deploy the Python MCP server (see `mcp_server/server.py`) as a custom MCP service.

### Option B: AI Gateway import
Best when your backend exposes standard REST endpoints and you want Alibaba Cloud to convert the API side into MCP-compatible access.

### Option C: remote HTTP MCP
Expose a remote MCP server yourself (recommended once stable), then connect/import that endpoint into supported clients/platforms.

## How "Alpha Pai" should fit

Treat Alpha Pai as a **provider adapter** first:
- call its REST or SDK endpoint from your backend
- normalize its output into your internal schema
- optionally wrap that as an internal tool
- only after that, decide whether to expose it outward through MCP


## Running the same wrapper through Manus API

If you already use Manus SaaS and want to keep its stronger agentic execution for long research jobs:

1. Upload/import your `.skill` into Manus first.
2. Use `GET /v2/skill.list` to find the Manus skill ID.
3. Set:

```bash
PROVIDER=manus
MANUS_API_KEY=...
MANUS_BASE_URL=https://api.manus.ai
MANUS_AGENT_PROFILE=manus-1.6-max
```

4. Call `/api/chat` with:

```json
{
  "message": "Research 凯莱英 6821.HK with full modern deep value structure",
  "stock_name": "凯莱英",
  "stock_code": "6821.HK",
  "provider": "manus",
  "manus_skill_ids": ["<your-skill-id>"]
}
```

The scaffold will create an async Manus task and return `task_id` + `task_url`.


## Manus waiting/approval flow

The Manus-backed version now includes a human-in-the-loop task loop instead of assuming every task is fully headless.

Added backend routes:

- `GET /api/manus/tasks/{task_id}` — poll task state, latest text, waiting metadata
- `POST /api/manus/tasks/{task_id}/confirm` — call Manus `confirmAction`
- `POST /api/manus/tasks/{task_id}/reply` — call Manus `sendMessage`

Added frontend behavior:

- Polls Manus task state every 5 seconds
- Shows a waiting/approval card when Manus pauses for confirmation
- Shows a text box when Manus asks the user for more input
- Resumes the same task instead of dead-ending the loop

### Required env for easiest Manus test

```bash
PROVIDER=manus
MANUS_API_KEY=...
MANUS_BASE_URL=https://api.manus.ai
MANUS_AGENT_PROFILE=manus-1.6-max
MANUS_FORCE_SKILLS=EUWYFTUYEFuweyfuoywfe1825e87EGQYWFVIUGvfggv
```

That lets the local web UI call Manus without manually passing the skill id every time.


This version reads `assistant_message.attachments` directly from the Manus task messages. In the sample payload, the final markdown report appears in `assistant_message.attachments[0]` with `filename` and `url`, so the backend proxies that URL and renders the markdown into the preview pane.
