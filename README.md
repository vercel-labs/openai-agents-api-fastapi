# Sandboxed Repo Agent

A FastAPI app that answers questions about public GitHub repositories using the OpenAI
Agents API public beta and Vercel Sandbox. It streams progress and returns an answer with
`RESULT.md`, `evidence.json`, and `events.jsonl` downloads.

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvercel-labs%2Fopenai-agents-api-fastapi&env=OPENAI_API_KEY,OPENAI_EXECUTOR_API_KEY&project-name=openai-agents-api-fastapi&repository-name=openai-agents-api-fastapi)

The deploy button requests both OpenAI keys. The public OpenAI Python SDK is installed
from PyPI; no private SDK repository access is needed.

## Local setup

You need Python 3.11 or later, Git, the Vercel CLI, a Vercel project with Sandbox access,
and an OpenAI project with [Agents API access](https://developers.openai.com/api/docs/guides/agents-api/quickstart).

```bash
git clone https://github.com/vercel-labs/openai-agents-api-fastapi.git
cd openai-agents-api-fastapi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

Set these values in `.env`:

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | Application key with `api.agents.read`, `api.agents.write`, and `api.responses.write` |
| `OPENAI_EXECUTOR_API_KEY` | Separate [environment key](https://platform.openai.com/agents?tab=environments&environment_view=keys); set other permissions to None |

Use keys from the same OpenAI organization, project, and user or service account.
The agent is defined inline for each session; no `AGENT_ID` is needed.

Link your Vercel project and pull its local Sandbox credentials:

```bash
vercel login
vercel link
vercel env pull .env.local
```

This supplies `VERCEL_OIDC_TOKEN` for local Sandbox calls. Refresh it with the same pull
command if it expires. The app loads `.env.local` before `.env`; remove blank or duplicate
OpenAI key entries from `.env.local` if they mask the values in `.env`. Keep both files untracked.
Deployed apps use the project's Vercel identity for Sandbox access.

Start FastAPI:

```bash
python -m uvicorn app:app --reload
```

Open [localhost:8000](http://localhost:8000), enter a public GitHub repository and a valid
branch, tag, or commit, and ask a focused question. Check `/api/health` for configuration
status; a completed inspection verifies the service connections.

For implementation details, see [ARCHITECTURE.md](ARCHITECTURE.md).
Coding agents should read [AGENTS.md](AGENTS.md).
