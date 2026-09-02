# Don't Forget

A deliberately small local MVP that remembers an intention, checks it later, and performs one bounded action. It has no server, dashboard, messaging integration, vector database, or multi-agent framework.

## What this milestone does

- Accepts a natural-language hackathon reminder.
- Reads a fixture source and persists a typed intention in SQLite.
- Keeps append-only `created`, `checked`, and `action_completed` events.
- Rechecks due intentions against the source and a local repository.
- Detects a near deadline and a missing demo.
- Creates `DEMO_CHECKLIST.md` only inside an explicitly allowed workspace.
- Reschedules the intention so the action and notification are not duplicated.

`DeterministicInterpreter` is a fixture-friendly replacement point for a future structured-output LLM. The core depends on its `Interpreter` protocol, not on a messaging channel or model provider.

## Setup

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```text
uv sync --extra test
uv run pytest
```

## Local demo

Create a text fixture such as:

```text
Hackathon: Tiny Agents
Deadline: 2026-09-03T11:00:00+00:00
Requirements:
- Public repository
- Demo video
```

The repository is considered public when it contains `.public`. A demo is present when it contains `DEMO.md`, `demo.mp4`, `demo.mov`, or `demo.webm`.

Start the local natural-language channel, limiting file actions to the project workspace:

```text
uv run dont-forget --workspace C:/path/to/project
```

Then send:

```text
don't let me forget this hackathon: file:///C:/path/to/hackathon.txt. my project is in C:/path/to/project
```

The immediate reply is `got it`. The process checks due intentions in the background. The deterministic MVP schedules its first check one hour later.

## Scope

The source reader uses Python's URL reader so the fixture can be a `file://` URL. Local actions reject repository paths outside the configured workspace. This milestone intentionally implements only the tested REMEMBER → CHECK → ACT path.
