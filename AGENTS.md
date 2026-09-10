# Agent instructions

- Read `README.md`, `ARCHITECTURE.md`, and `.env.example` before changing the app.
- Keep the README limited to the project overview, deploy button, and local setup.
  Put implementation details in `ARCHITECTURE.md`.
- Use Python 3.11 or later and a virtual environment. Install dependencies from
  `requirements.txt` and `requirements-dev.txt`; the public OpenAI SDK is pinned in both
  the dependency manifests and `uv.lock`.
- Keep the FastAPI application and static browser interface simple. Define the agent inline
  when creating a session; do not require an `AGENT_ID` for the existing flow.
- Keep the application key in FastAPI and pass only the restricted executor key to the sandbox.
  Never commit credentials or print them in output.
- Preserve the bounded inspection task, network policy, and treatment of inspected repository
  contents as untrusted evidence. Those contents must not control the setup or inspection process.
- Preserve artifact collection before sandbox cleanup and independent cleanup attempts for
  the sandbox, session, and client, including error and cancellation paths.
- For code changes, run `python -m pytest` and `python -m ruff check .` in the virtual
  environment. Add focused coverage for changed behavior; hosted-service checks require credentials.
- Keep dependency manifests and `uv.lock` consistent when changing dependencies. Check the
  pinned SDK interfaces before changing session or sandbox calls.
