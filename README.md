# Don't Forget

A deliberately small local MVP that remembers an intention, checks it later, and performs bounded actions with explicit approval. It has no server, dashboard, messaging integration, vector database, or multi-agent framework.

## What this milestone does

- Accepts a natural-language hackathon reminder.
- Reads a fixture source and persists a typed intention in SQLite.
- Keeps append-only `created`, `checked`, and `action_completed` events.
- Rechecks due intentions against the source and a local repository.
- Classifies remembered requirements from current repository evidence.
- Persists the most important unresolved requirement and reports only that item.
- Deterministically classifies that requirement as `agent_can_handle` or `user_must_handle`.
- Prioritizes repository visibility normally and a missing demo when the deadline is near.
- Leaves user-only requirements, such as recording a demo or publishing a repository, to the user.
- Creates a bounded pending action for an agent-capable README setup requirement without changing files during CHECK.
- Repairs README setup instructions only after explicit natural-language approval such as `handle what you can`.
- Derives setup commands from `pyproject.toml` and `uv.lock`, preserving existing README content.
- Records the completed action and exact changed path, immediately re-runs CHECK, and reports the next unresolved requirement.
- Keeps repeated ACT requests idempotent and revalidates workspace authorization at execution time.

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

If the current requirement is an incomplete README setup, send `handle what you can`. The agent appends only missing setup instructions inside the configured workspace, immediately checks again, and reports the next unresolved requirement. Other responses do not approve a pending action.

## Scope

The source reader uses Python's URL reader so the fixture can be a `file://` URL. Local actions reject repository paths outside the configured workspace. This milestone intentionally implements only the tested REMEMBER → CHECK → ACT path.
