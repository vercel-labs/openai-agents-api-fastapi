# Architecture

## Overview

The application serves a static browser interface and an asynchronous inspection endpoint
through FastAPI. Every inspection owns a new OpenAI Agents API public beta session and a new
Vercel Sandbox. The browser connection carries progress and the final result; the app has
no database, job queue, saved agent, or persistent conversation history.

```text
Browser -- POST /api/inspect --> FastAPI -- create / stream --> Agents API session
   ^                               |                              ^
   | server-sent events            | create sandbox                | outbound connection
   |                               v                              |
   +-------------------------- Vercel Sandbox: cloned repo + Codex exec-server
```

The FastAPI process owns the session lifecycle and collects results. The Codex executor
runs commands inside the sandbox. These are separate execution environments.

## Modules

| File | Responsibility |
| --- | --- |
| `app.py` | Root entry point for Vercel and local Uvicorn; exports the app from `sandbox_agent/main.py` |
| `sandbox_agent/main.py` | FastAPI routes, static assets, configuration check, streaming response |
| `sandbox_agent/models.py` | Request validation and artifact response model |
| `sandbox_agent/config.py` | Environment loading, required keys, model settings, lifecycle limits |
| `sandbox_agent/runner.py` | Session creation, event forwarding, executor health checks, result assembly, cleanup |
| `sandbox_agent/vercel_sandbox.py` | Sandbox creation, repository checkout, Codex installation and executor startup |
| `sandbox_agent/prompt.py` | Inspection instructions, command budget, workspace and artifact paths |
| `sandbox_agent/artifacts.py` | Evidence validation, event log, artifact collection and fallbacks |
| `sandbox_agent/sse.py` | Server-sent event encoding |
| `sandbox_agent/static/` | HTML, CSS, browser stream parser, result and download rendering |
| `tests/` | Tests using fake sessions and sandboxes |

## Request lifecycle

1. The browser sends a repository URL, Git ref, and question to `POST /api/inspect`.
   The request model rejects unknown fields, checks the HTTPS GitHub URL, and rejects
   empty refs, refs beginning with a dash, and control characters. The app is intended
   for public repositories; URL validation alone does not establish repository visibility
   or whether the selected ref exists. The sandbox clone must succeed.
2. FastAPI loads settings and requires two nonempty, distinct OpenAI keys. The runner
   creates an `AsyncOpenAI` client with the application key and configured base URL.
3. `client.beta.agents.sessions.create` receives an inline agent definition containing the model,
   instructions, reasoning effort, and text verbosity. Its environment is `self_hosted`
   with workspace `/vercel/sandbox`. No saved agent or `AGENT_ID` is required.
4. The app creates a Vercel Sandbox with `GitSource`, the requested revision, a shallow
   clone, the `node24` runtime, and an outbound network allowlist. Node supplies npm for
   executor installation; the web application remains Python.
5. Startup installs the configured Codex package, creates `/tmp/sandbox-agent`, and writes
   the inspection prompt there. It starts `codex exec-server` as a detached command with
   `session.environment.id`, the unchanged `session.environment.remote_url`, and the
   executor key passed as `CODEX_API_KEY`.
   The executor connects outbound; no inbound sandbox port is exposed.
6. The runner submits the prompt through the SDK async context manager
   `client.beta.agents.sessions.stream(session.id, input=prompt)`. The SDK subscribes
   before sending input. The runner records API events and forwards activity and
   available answer deltas. Complete text events replace the corresponding content
   part so missing deltas or repeated final text do not corrupt the fallback answer. It checks sandbox and
   executor health while opening and consuming the stream, then retrieves the final session status.
7. The agent writes `RESULT.md` and `evidence.json`. FastAPI writes the captured event log
   to `events.jsonl` and reads the answer and evidence. It uses streamed text or an
   incomplete-result message when needed.
8. The runner attempts to delete the session, stop the sandbox, and close the client.
   It then emits the result and completion events. A completed root-agent turn followed by an `idle` session with no run
   error counts as success; cleanup warnings are reported separately.

The session ID, environment ID, and sandbox ID identify different resources. The environment
ID connects the executor to the session; the response exposes the session and sandbox IDs.

## HTTP and streaming interface

| Route | Behavior |
| --- | --- |
| `GET /` | Browser interface |
| `GET /assets/...` | Static assets |
| `GET /api` | Service identity |
| `GET /api/health` | Required configuration status, model and reasoning effort when configured |
| `POST /api/inspect` | Inspection event stream |
| `GET /api/docs` | Interactive API documentation |
| `GET /api/openapi.json` | OpenAPI schema |

A health response of `ready` only confirms settings are present and distinct. It does not
validate OpenAI credentials, Agents API access, or Sandbox connectivity.

Example inspection payload:

```json
{
  "repo_url": "https://github.com/vercel-labs/openai-agents-api-fastapi",
  "ref": "main",
  "question": "What framework does this repository use? Cite the relevant files."
}
```

FastAPI returns `text/event-stream`. The browser uses streaming `fetch` for the POST request
and parses `status`, `activity`, `answer_delta`, `error`, `warning`, `result`, and `complete`
events. Validation errors are rendered as readable messages, and a stream closing
without `complete` is reported as interrupted. The result contains the answer, structured evidence, artifact contents, model,
resource IDs, duration, and cleanup warnings. The browser creates downloadable blobs from
the artifact contents and renders answers as text.

## Credentials and configuration

The app loads `.env.local` and then `.env` without replacing existing environment values.
Process environment values take precedence; a blank value loaded first can mask a later one.

| Setting | Role |
| --- | --- |
| `OPENAI_API_KEY` | Creates and manages sessions from FastAPI; never passed to the sandbox |
| `OPENAI_EXECUTOR_API_KEY` | Restricted executor key passed only to Codex inside the sandbox |
| `VERCEL_OIDC_TOKEN` | Local Sandbox authentication; deployments use project identity |
| `AGENT_API_BASE_URL` | OpenAI API root override; defaults to `https://api.openai.com/v1` |
| `AGENT_MODEL`, `AGENT_REASONING_EFFORT` | Inline agent configuration |
| `CODEX_PACKAGE` | Executor package installed during startup |
| `SANDBOX_ALLOWED_DOMAINS` | Sandbox outbound network allowlist |

See `.env.example` for defaults. The public SDK is pinned to `openai==3.13.0` from PyPI.
The SDK adds `/agents` routes and the `OpenAI-Beta: agents=v1` header. Legacy
`AGENT_API_BASE_URL` values ending in `/agents` are normalized to the API root so
existing environment overrides keep working. The executor URL always comes from
the session response, never from the configured API root.

The application key needs `api.agents.read`, `api.agents.write`, and
`api.responses.write`. Create the executor key on the Agents dashboard with all
other permissions set to None. Both keys must share an organization, project, and
user or service account.

The lockfile retains existing dependency versions where possible. Its narrow OpenAI
release-date exception permits the pinned launch release under local minimum-age policies.
The integration uses the pinned `vercel==0.7.1` SDK interfaces; verify method signatures
before changing dependencies. Vercel hosts the FastAPI app as a native Python function.
No `vercel.json` is required: the Python runtime includes the static assets by default,
and Fluid compute defaults to a maximum duration of 300 seconds. The Vercel project's
framework preset must be FastAPI, with build, install, development, output, and root
directory overrides cleared. An existing project set to Other does not automatically
switch to FastAPI when redeployed; it can report a successful deployment while returning
404 for application routes. Set the preset with `vercel project update --framework fastapi`.

## Execution boundaries and limits

The inspection instructions treat repository content as untrusted evidence and prohibit
editing the checkout, dependency installation, builds, servers, and network calls during
inspection. They request at most eight short commands and 120 seconds of investigation.
These are agent instructions, not a read-only filesystem mount or a hard command counter.
Application startup separately installs Codex before inspection begins.

The sandbox network policy restricts outbound access to the configured OpenAI, GitHub,
and npm-related hosts. The executor still has shell access inside the sandbox.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SESSION_CREATE_TIMEOUT_SECONDS` | 20 | Session creation |
| `SANDBOX_STARTUP_TIMEOUT_SECONDS` | 90 | Sandbox and executor startup |
| `STREAM_EXECUTION_TIMEOUT_SECONDS` | 150 | Event-stream execution |
| `STREAM_NO_PROGRESS_TIMEOUT_SECONDS` | 45 | Maximum time without an API event |
| `EXECUTOR_HEALTHCHECK_INTERVAL_SECONDS` | 5 | Health polling interval while streaming |
| `SANDBOX_TIMEOUT_MS` | 240000 | Sandbox lifetime limit |
| `CLEANUP_TIMEOUT_SECONDS` | 30 | Each bounded cleanup operation |
| `ARTIFACT_MAX_BYTES` | 200000 | Per-artifact content truncation threshold |

Each artifact exceeding its threshold is truncated and marked. A truncated JSON or JSONL
artifact may no longer parse. The threshold applies when assembling downloads, not to total
memory consumed while capturing the event stream or reading sandbox files.

## Failure handling and cleanup

The runner reports errors, attempts to cancel a failed session, and returns fallback artifacts
when collection fails. Malformed evidence becomes a structured limitation rather than a parse
error. Application and executor key values are redacted from normal run-error messages.

Cancellation uses an `agent.session.input.cancel` input event; closing the SDK stream
does not cancel the backend turn. Generic SDK errors are recorded as redacted
`inspection.error` entries alongside API events.

Cleanup attempts session deletion by ID, sandbox stop, and `client.close()` independently,
with a separate timeout for each. Delete while the executor is connected: stopping it
first can move the beta session into `requires_action`, which blocks deletion. Transient
delete conflicts after cancellation are retried within the cleanup timeout. The outer stream wrapper cancels unfinished work,
attempts bounded artifact collection, and invokes cleanup in `finally` when iteration
exits. This finalization is shielded from the response cancellation scope. Startup also
stops an allocated sandbox on timeout or cancellation. Artifacts collected after browser
disconnection are not delivered or persisted outside the sandbox. Cleanup is best effort; a warning means a resource
operation failed and may need follow-up. The sandbox lifetime limit bounds its runtime.

The app keeps results in memory and returns them to the browser. It does not resume runs
after disconnection or provide durable storage. Longer tasks, follow-up turns, and persisted
reports require a background lifecycle controller and storage.

## Verification

### Public beta migration decisions

Reviewed against the official [self-hosted environment guide](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted),
[Vercel integration guide](https://developers.openai.com/api/docs/guides/agents-api/environments/providers/vercel),
and [event handling guide](https://developers.openai.com/api/docs/guides/agents-api/sessions/events)
on September 11, 2026. The integration keeps the inline agent, `gpt-5.6-sol` model,
application-managed sandbox, and bounded inspection flow. The migration changes the
SDK and protocol integration without adding saved agents, webhooks, or persistent runs.

The pinned Python SDK's `sessions.stream` helper subscribes before submitting input;
each inspection is its session's only writer. Local Sandbox authentication continues
to use the Vercel OIDC token pulled by the CLI. Real local HTTP and browser inspections
passed with the public beta, including all three artifact downloads and no cleanup warnings.

### Automated checks

With dependencies installed in the active virtual environment:

```bash
python -m pytest
python -m ruff check .
```

Tests cover request validation, configuration, prompts, streaming, artifact handling, and
sandbox lifecycle behavior. Runner tests use the actual public SDK with a mocked HTTP
transport and sandbox, covering request paths and beta headers, subscription before input,
turn outcomes, missing deltas, premature stream closure, timeouts, executor failures,
client cancellation, and independent cleanup. They do not exercise the hosted services. Verify a
real integration by submitting a small public-repository question and checking the answer,
resource IDs, downloads, and cleanup outcome.

For browser stream parsing and form-error regression tests (Node.js required):

```bash
node --test tests/test_ui.mjs
```
