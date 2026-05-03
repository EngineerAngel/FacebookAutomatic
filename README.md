# Facebook Groups Auto-Poster

Multi-account automation system for posting to Facebook Groups, orchestrated by **OpenClaw**.

## Quick Start

```bash
pip install -r facebook_auto_poster/requirements.txt
copy facebook_auto_poster\.env.example facebook_auto_poster\.env  # fill credentials
python facebook_auto_poster/main.py
# → http://0.0.0.0:5000
```

Open `http://localhost:5000/admin` to manage accounts and publish.

## Architecture

- **REST API** (Flask) — receives jobs from OpenClaw via `POST /post` with `X-API-Key`
- **Browser automation** — Patchright (patched Chromium) + Emunium (OS-level mouse)
- **Async pipeline** — `asyncio.gather` over N accounts, `asyncio.Semaphore` for concurrency
- **SQLite WAL** — accounts, jobs, cookies, login history

See [CLAUDE.md](CLAUDE.md) for full architecture reference.

## Documentation

| Document | Contents |
|----------|----------|
| [CLAUDE.md](CLAUDE.md) | Architecture, data flow, DB schema, API reference, gotchas |
| [docs/testing.md](docs/testing.md) | Integration testing with OpenClaw (curl + Python script) |
| [docs/metrics.md](docs/metrics.md) | Prometheus + Grafana setup and usage |
| [plan/README.md](plan/README.md) | Improvement plan index — phases, specs, decisions |

## Project Structure

```
facebook_auto_poster/   ← application code
plan/                   ← improvement plan, specs, decisions
docs/                   ← operational guides
monitoring/             ← Prometheus + Grafana config
```

## Tests

```bash
python -m pytest facebook_auto_poster/tests/ -q
```
