# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt

python app.py                 # local dev server (PORT env var, defaults to 5000)
gunicorn app:app              # how it runs in production (Render)
```

There is no test suite, linter config, or CI in this repo. Verification is done by hitting the endpoints:

```bash
curl localhost:5000/api/health

curl -X POST localhost:5000/api/fireflies-webhook/<client_id> \
  -H 'Content-Type: application/json' \
  -d '{"event":"meeting.summarized","meeting_id":"<transcript_id>"}'
```

Note that a real `meeting_id` triggers a background job that can block for ~10 minutes on Fireflies retries; watch the logs rather than the HTTP response.

## Environment

`SUPABASE_URL` and `SUPABASE_SECRET_KEY` only. Both are read at import time and `create_client` runs at module scope, so the app fails to start without them. Per-client Fireflies/Notion credentials are **not** env vars — they live in Supabase (see below). Two tables are used: `clients` and `meeting_jobs`.

## Architecture

Single-file Flask app (`app.py`) implementing one pipeline: **Fireflies webhook → Fireflies GraphQL API → Notion page**.

**Multi-tenancy via URL path.** The webhook route is `/api/fireflies-webhook/<client_id>`. Each client registers their own webhook URL with that segment; `get_client_credentials` looks the id up in the Supabase `clients` table and reads `fireflies_api_key`, `notion_api_key`, and `notion_database_id` off the row. Adding a client is a database insert, not a code change. Any new per-client setting belongs on that table, not in the environment. Because a half-filled row is the likeliest onboarding mistake, `process_meeting_in_background` runs `unset_credentials` before any network call and fails the job immediately when `fireflies_api_key`, `notion_api_key`, or `notion_database_id` is empty or still a placeholder (`your_...`, `<...>`, `changeme`); without that check an un-onboarded client burns the full ~10 minute Fireflies backoff window before reporting a failure that was knowable up front. Keep the placeholder hints narrow — a false positive refuses a client that would have worked.

**Everything returns HTTP 200.** Unknown client, malformed payload, unhandled exception — the webhook logs and returns 200. This is deliberate: Fireflies retries non-2xx responses, and a retry storm is worse than a dropped event. Preserve this when editing the handler.

**Slow work is detached from the request.** `meeting.summarized` fires before Fireflies has finished writing the summary, so `fetch_fireflies_summary` polls with exponential backoff (8s → capped at 120s, 9 attempts, ~10 min total) until `summary.overview` is non-empty. That can't happen inside the request without tripping Fireflies' and Gunicorn's timeouts, so the handler spawns a daemon `threading.Thread` (`process_meeting_in_background`) and returns immediately.

The payload `meeting_id == "test_00000000"` is Fireflies' webhook-verification ping and is skipped.

**Duplicate deliveries are deduped in `meeting_jobs`.** Before spawning the thread, the handler calls `claim_meeting_job`, which inserts a `pending` row keyed `(client_id, meeting_id)`. That pair is a unique constraint, so a delivery that races past the existence check loses on the insert and is dropped; the handler returns `{"status": "duplicate"}` and still answers 200. `process_meeting_in_background` closes the row out as `success` or `failed`. Two kinds of row are **re-claimable** — a later redelivery flips them back to `pending` and runs again: a `failed` row, so a Fireflies retry can heal a bad run, and a `pending` row older than `STALE_PENDING_AFTER` (20 minutes), whose worker died with the process. A `success` row, or a `pending` row still inside that window, is skipped. Re-claiming writes the row, which bumps `updated_at` through the trigger and re-arms the staleness window for the new worker, so the age is always measured from the most recent claim rather than from first delivery. Dedupe is scoped per client, not globally by `meeting_id`, because two client rows can share one Fireflies workspace. Bookkeeping never blocks the pipeline: if Supabase errors for any reason other than the unique violation, `claim_meeting_job` logs it and the meeting is processed anyway. Schema lives in `migrations/0001_meeting_jobs.sql`; `updated_at` is set by a database trigger, so don't send it from the app.

Consequences worth knowing before changing this area: in-flight jobs are still lost on process restart or deploy, and their row is left at `pending` until the staleness window expires — nothing re-drives it on its own, so recovery depends on Fireflies redelivering that meeting. Failures after the retry window are visible both in the logs and as `failed` rows in `meeting_jobs`.

`STALE_PENDING_AFTER` must stay comfortably above the worst-case runtime of `process_meeting_in_background`, which is roughly 12.5 minutes (~10 min of backoff sleeps plus up to 9 x 15s request timeouts) before the Notion write. The 20-minute setting leaves about 7.5 minutes of margin. Shortening it, or lengthening the Fireflies retry window without raising it, risks a redelivery stealing a job that is still running and writing a second Notion page for the meeting.

**Notion output shape.** `push_to_notion` creates a page whose properties are `Name` (title), `Meeting ID` (rich_text), and optionally `Date` (date, from a ms epoch or ISO string). The body is a fixed `Summary` / `Action Items` two-heading layout; `text_to_blocks` splits text on newlines and strips `-*•` prefixes into bulleted list items or unchecked to-dos, and `parse_rich_text` converts `**...**` into bold runs. Client Notion databases must already have those three properties — property renames break every client at once.

## Logging

All log lines are tagged by stage: `[Fireflies Webhook]`, `[Fireflies Fetch]`, `[Fireflies Background]`, `[Meeting Jobs]`, and include `client=` / `meeting=`. Keep that convention — it is the only way to trace a request once it hands off to the background thread. Log the *shape* of a summary (title, character counts), never its text: transcripts carry whatever was said in the meeting, and Render retains these logs indefinitely.
