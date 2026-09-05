# Don't Forget

A deliberately small local MVP that remembers an intention, checks it later, and performs bounded actions with explicit approval. It has no server, dashboard, messaging integration, vector database, or multi-agent framework.

## What this milestone does

- Accepts everyday wording such as `I need to ...`, `remind me to ...`, or a URL followed by `don't let me forget this`, with an optional local project path.
- Persists the user's stated objective even when no source is available; unsupported user work stays pending and is never reported as performed.
- Preserves the source URL and original message in the stored intention.
- Prefers an explicit action such as applying or registering; otherwise infers participation from clear event context.
- Stores ambiguous sources as low-confidence follow-ups instead of inventing deadlines, requirements, or actions.
- Boundedly fetches up to 200 KB per source with an injectable fetcher, then runs an injectable extractor.
- Allows network fetching only for HTTP(S) destinations that resolve to public addresses, pins the connection to a validated address, rejects redirects, and limits `file://` sources to approved roots.
- Extracts only explicit timezone-aware ISO deadlines and list items under clear requirements, eligibility, participation, or submission headings.
- Stores the source excerpt, URL, observation time, and confidence behind verified deadlines, requirements, and title context.
- Leaves unsupported deadlines and requirements unknown; inferred objectives remain visibly distinct from verified facts.
- Schedules and refreshes source-only CHECKs when a verified future deadline exists, without requiring a repository.
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
- Understands natural status questions such as `am I forgetting anything?` and refreshes evidence before answering.
- Resolves references such as `what about Tiny Agents?` only when they identify one existing item; otherwise it asks rather than guesses.
- Lets a later `my project is in ...` message add repository context when there is exactly one eligible open item.
- Supports scoped approval such as `handle what you can for Tiny Agents` and broad approval across all currently proposed, supported actions.
- Derives setup commands from `pyproject.toml` and `uv.lock`, preserving existing README content.
- Records the completed action and exact changed path, immediately re-runs CHECK, and reports the next unresolved requirement.
- Keeps repeated and concurrent ACT requests idempotent through an atomic persisted claim, revalidates workspace authorization at execution time, and leaves a failed post-ACT CHECK retryable.
- Carries one intention ID and snapshot through REMEMBER, persisted CHECK refreshes, approved ACT, immediate post-ACT CHECK, and later repeated operations.

`DeterministicInterpreter` is a fixture-friendly replacement point for a future structured-output LLM. The core depends on its `Interpreter` protocol, not on a messaging channel or model provider.

For example, `https://example.com/hackathon — don't let me forget this` receives the minimal response `got you.`. A source-only intention is enriched from explicit source evidence. If no deadline is verified, it remains unscheduled; if one is verified, CHECK refreshes the source on a deadline-bounded schedule. The existing repository-aware hackathon flow continues to use approval-gated ACT.

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
uv run dont-forget --workspace C:/path/to/project --source-root C:/path/to
```

Then text it naturally, for example:

```text
don't let me forget this hackathon: file:///C:/path/to/hackathon.txt. my project is in C:/path/to/project
```

The same repository-aware flow also accepts wording such as `Remember to submit my project. Use file:///C:/path/to/hackathon.txt for the rules. My project is in C:/path/to/project`. A source is optional for plain intentions such as `Remember to call the dentist`; without verified evidence or an authorized capability, the objective remains explicitly pending and is not scheduled or executed.

The immediate reply is `got it`. The process checks due intentions in the background. For repository-aware intentions with a verified future deadline, the deterministic MVP schedules its first check one hour later.

You can ask `am I forgetting anything?`, `what did I forget?`, or `what about Tiny Agents?`. These questions refresh the selected source and repository evidence before producing a short answer. If a reference is missing or matches more than one item, the agent asks which one instead of choosing silently.

If the current requirement is an incomplete README setup, send the standalone approval `handle what you can` (optionally prefixed with `please` or suffixed with `safely`). The agent appends only missing setup instructions inside the configured workspace, immediately checks again, and reports the next unresolved requirement. Embedded or negated uses of that phrase do not approve a pending action.

## Scope

An approved fixture can be a `file://` URL. HTTP(S) hosts must resolve only to public addresses, the connection is pinned to a validated address while retaining the original Host and TLS identity, redirects are rejected, local files must be under configured source roots, and reads are capped at 200 KB with a five-second timeout. The deterministic extractor intentionally recognizes only explicit timezone-aware ISO 8601 deadlines and clearly headed list requirements; JavaScript-rendered pages, natural-language dates, PDFs, redirects, and semantic extraction need a future web/Hermes-backed adapter. Local actions reject repository paths outside the configured workspace. This milestone intentionally implements only the tested REMEMBER → CHECK → ACT path.
