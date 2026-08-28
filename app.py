import os
import json
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as ext_requests
from notion_client import Client as NotionClient
from supabase import create_client, Client as SupabaseClient

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")  # <-- placeholder, set real value in Render env vars only

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def get_client_credentials(client_id):
    """Look up a client's stored credentials from Supabase using their client_id."""
    result = supabase.table("clients").select("*").eq("client_id", client_id).execute()
    if not result.data:
        return None
    return result.data[0]


# Every credential a client row must supply before the pipeline can do anything.
REQUIRED_CLIENT_FIELDS = ("fireflies_api_key", "notion_api_key", "notion_database_id")

# Substrings that mark a column as never filled in. Kept narrow on purpose: a
# false positive here refuses to process a client that would have worked.
CREDENTIAL_PLACEHOLDER_HINTS = ("placeholder", "changeme", "change_me")


def _looks_unset(value):
    """True when a credential column is empty or still holds a setup placeholder."""
    if not isinstance(value, str) or not value.strip():
        return True
    lowered = value.strip().lower()
    if lowered.startswith("your_") or lowered.startswith("<"):
        return True
    return any(hint in lowered for hint in CREDENTIAL_PLACEHOLDER_HINTS)


def unset_credentials(client):
    """Names of the credential columns this client row has not really filled in."""
    return [field for field in REQUIRED_CLIENT_FIELDS if _looks_unset(client.get(field))]


# A 'pending' row is a live claim, but the worker holding it dies with the
# process (deploy, restart, crash). Past this age we assume the worker is gone
# and let a redelivery take the job over. Keep it comfortably above the worst
# case runtime of process_meeting_in_background (~12.5 min: fetch_fireflies_summary
# burns ~10 min of backoff sleeps plus up to 9 x 15s request timeouts, then the
# Notion write) or a redelivery can steal a job that is still running and write
# a second Notion page for the meeting.
STALE_PENDING_AFTER = timedelta(minutes=20)


def _claim_age(row):
    """How long ago the row was last claimed, or None if the timestamp is unreadable."""
    raw = row.get("updated_at") or row.get("created_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - stamp


def _is_duplicate_key_error(error):
    """Postgres unique_violation (23505) - another delivery claimed this meeting first."""
    return getattr(error, "code", None) == "23505" or "23505" in str(error)


def claim_meeting_job(client_id, meeting_id):
    """
    Reserve a meeting for processing by inserting a 'pending' row in meeting_jobs.

    Returns False when an earlier delivery already claimed it, which is the
    signal to drop this webhook as a duplicate. Two kinds of row are instead
    re-claimable, flipped back to 'pending' and processed again:

      - 'failed', so a Fireflies retry can heal a bad run;
      - 'pending' but older than STALE_PENDING_AFTER, so a job whose worker
        died with the process does not block the meeting forever.

    Re-claiming writes the row, which bumps updated_at via the database trigger
    and so re-arms the staleness window for the new worker.

    Bookkeeping never blocks the pipeline: if Supabase fails for any reason
    other than the unique constraint, we log it and process the meeting anyway.
    """
    try:
        existing = (
            supabase.table("meeting_jobs")
            .select("status,updated_at,created_at")
            .eq("client_id", client_id)
            .eq("meeting_id", meeting_id)
            .execute()
        )

        if existing.data:
            row = existing.data[0]
            status = row.get("status")
            age = _claim_age(row)

            if status == "failed":
                reason = "an earlier failure"
            elif status == "pending" and age is not None and age > STALE_PENDING_AFTER:
                reason = f"a stale pending claim ({int(age.total_seconds() // 60)}m old)"
            else:
                age_note = "age unknown" if age is None else f"{int(age.total_seconds())}s old"
                logging.info(
                    f"[Meeting Jobs] client={client_id} meeting={meeting_id} "
                    f"already claimed (status={status}, {age_note})"
                )
                return False

            (
                supabase.table("meeting_jobs")
                .update({"status": "pending"})
                .eq("client_id", client_id)
                .eq("meeting_id", meeting_id)
                .execute()
            )
            logging.info(f"[Meeting Jobs] client={client_id} meeting={meeting_id} re-claimed after {reason}")
            return True

        supabase.table("meeting_jobs").insert({
            "client_id": client_id,
            "meeting_id": meeting_id,
            "status": "pending"
        }).execute()
        return True

    except Exception as e:
        if _is_duplicate_key_error(e):
            # Two deliveries raced past the select; the other one won the insert.
            logging.info(f"[Meeting Jobs] client={client_id} meeting={meeting_id} claimed by a concurrent delivery")
            return False
        logging.error(f"[Meeting Jobs] client={client_id} meeting={meeting_id} claim failed: {e}")
        return True


def update_meeting_job(client_id, meeting_id, status):
    """Mark a job 'success' or 'failed'. updated_at is maintained by a database trigger."""
    try:
        (
            supabase.table("meeting_jobs")
            .update({"status": status})
            .eq("client_id", client_id)
            .eq("meeting_id", meeting_id)
            .execute()
        )
        logging.info(f"[Meeting Jobs] client={client_id} meeting={meeting_id} marked {status}")
    except Exception as e:
        logging.error(f"[Meeting Jobs] client={client_id} meeting={meeting_id} could not mark {status}: {e}")


# Fireflies error bodies are short, but a stray HTML error page from a proxy is
# not — cap what we put in the logs.
MAX_LOGGED_BODY = 2000


def _log_body(response):
    """Response body as a single log-safe line, truncated."""
    body = " ".join((response.text or "").split())
    if len(body) > MAX_LOGGED_BODY:
        return body[:MAX_LOGGED_BODY] + f"... [truncated, {len(body)} chars total]"
    return body or "<empty body>"


def fetch_fireflies_summary(meeting_id, fireflies_api_key, max_retries=9, initial_delay=8, max_delay=120):
    """
    Fetches the transcript summary for a given meeting_id from Fireflies.
    Retries with exponential backoff (capped at max_delay) if Fireflies
    returns an error or an empty summary (common right after the
    meeting.summarized webhook fires, since the summary can still be
    writing on Fireflies' side — this can occasionally take several
    minutes). Default settings give roughly a 10-minute total window
    before giving up: 8s, 16s, 32s, 64s, 120s, 120s, 120s, 120s.
    """
    query = """
    query Transcript($transcriptId: String!) {
        transcript(id: $transcriptId) {
            title
            date
            summary {
                overview
                action_items
            }
        }
    }
    """
    headers = {
        "Authorization": f"Bearer {fireflies_api_key}",
        "Content-Type": "application/json"
    }

    delay = initial_delay
    last_error = None

    for attempt in range(1, max_retries + 1):
        retry_note = f"retrying in {delay}s" if attempt < max_retries else "no attempts left"
        try:
            response = ext_requests.post(
                "https://api.fireflies.ai/graphql",
                headers=headers,
                json={"query": query, "variables": {"transcriptId": meeting_id}},
                timeout=15
            )
            # Fireflies puts the real reason (bad key, unknown transcript, plan
            # limits) in the body, including on 5xx — log it before raise_for_status
            # throws the response away.
            if not response.ok:
                logging.error(
                    f"[Fireflies Fetch] meeting={meeting_id} HTTP {response.status_code} on attempt {attempt}, "
                    f"body: {_log_body(response)}"
                )
            response.raise_for_status()
            data = response.json()

            # A GraphQL error can also arrive as HTTP 200 with data: null, which
            # otherwise looks identical to "summary not ready yet".
            if data.get("errors"):
                logging.error(
                    f"[Fireflies Fetch] meeting={meeting_id} GraphQL errors on attempt {attempt}, "
                    f"body: {_log_body(response)}"
                )

            transcript = (data.get("data") or {}).get("transcript")
            summary = transcript.get("summary") if transcript else None

            if summary and summary.get("overview"):
                logging.info(f"[Fireflies Fetch] meeting={meeting_id} succeeded on attempt {attempt}")
                return transcript

            # Got a response but summary isn't ready yet — retry
            logging.warning(f"[Fireflies Fetch] meeting={meeting_id} summary not ready on attempt {attempt}, {retry_note}")

        except ext_requests.exceptions.RequestException as e:
            last_error = e
            logging.warning(f"[Fireflies Fetch] meeting={meeting_id} error on attempt {attempt}: {e}, {retry_note}")

        if attempt < max_retries:
            time.sleep(delay)
            delay = min(delay * 2, max_delay)  # exponential backoff, capped at max_delay

    logging.error(f"[Fireflies Fetch] meeting={meeting_id} failed after {max_retries} attempts")
    raise RuntimeError(f"Fireflies summary not available for {meeting_id} after {max_retries} retries") from last_error


def parse_rich_text(text):
    parts = text.split("**")
    rich_text = []
    for i, part in enumerate(parts):
        if not part:
            continue
        rich_text.append({
            "type": "text",
            "text": {"content": part},
            "annotations": {"bold": i % 2 == 1}
        })
    if not rich_text:
        rich_text = [{"type": "text", "text": {"content": text}}]
    return rich_text


def text_to_blocks(text, block_type):
    blocks = []
    if not text:
        return blocks
    lines = str(text).strip().split("\n")
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        content = stripped.lstrip("-*•").strip()
        if not content:
            continue
        if block_type == "to_do":
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": parse_rich_text(content), "checked": False}
            })
        else:
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": parse_rich_text(content)}
            })
    return blocks


def push_to_notion(meeting_data, meeting_id, notion_api_key, notion_database_id):
    if not meeting_data:
        return None

    notion = NotionClient(auth=notion_api_key)

    title = meeting_data.get("title") or "Untitled Meeting"
    summary = meeting_data.get("summary") or {}
    overview = summary.get("overview") or ""
    action_items = summary.get("action_items") or ""
    raw_date = meeting_data.get("date")

    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Meeting ID": {"rich_text": [{"text": {"content": str(meeting_id)}}]}
    }

    if raw_date:
        try:
            if isinstance(raw_date, (int, float)):
                iso_date = datetime.utcfromtimestamp(raw_date / 1000).isoformat()
            else:
                iso_date = str(raw_date)
            properties["Date"] = {"date": {"start": iso_date}}
        except Exception:
            pass

    summary_blocks = text_to_blocks(overview, "bulleted_list_item")
    if not summary_blocks:
        summary_blocks = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": "No summary available."}}]}
        }]

    action_blocks = text_to_blocks(action_items, "to_do")
    if not action_blocks:
        action_blocks = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": "None"}}]}
        }]

    children = (
        [{"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Summary"}}]}}]
        + summary_blocks
        + [{"object": "block", "type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": "Action Items"}}]}}]
        + action_blocks
    )

    notion.pages.create(
        parent={"database_id": notion_database_id},
        properties=properties,
        children=children
    )


def process_meeting_in_background(client_id, client, meeting_id):
    """
    Runs the slow part (fetch summary with retries + push to Notion) on a
    background thread, completely independent of the webhook request/response
    cycle, so retries can take as long as they need without Fireflies or
    Gunicorn timing out the original request.
    """
    try:
        # A row that was never filled in cannot succeed, and retrying it burns the
        # full ~10 minute Fireflies backoff window before saying so. Fail now, with
        # the column names, so onboarding mistakes are obvious in the logs.
        unset = unset_credentials(client)
        if unset:
            raise RuntimeError(
                f"client row has no usable {', '.join(unset)} "
                f"(empty or still a setup placeholder)"
            )

        meeting_data = fetch_fireflies_summary(meeting_id, client["fireflies_api_key"])
        logging.info(f"[Fireflies Background] client={client_id} fetched summary: {json.dumps(meeting_data)}")
        push_to_notion(
            meeting_data,
            meeting_id,
            client["notion_api_key"],
            client["notion_database_id"]
        )
        logging.info(f"[Fireflies Background] client={client_id} pushed to Notion successfully")
        update_meeting_job(client_id, meeting_id, "success")
    except Exception as e:
        logging.error(f"[Fireflies Background] client={client_id} meeting={meeting_id} failed: {e}")
        update_meeting_job(client_id, meeting_id, "failed")


@app.route("/api/fireflies-webhook/<client_id>", methods=["POST"])
def fireflies_webhook(client_id):
    try:
        payload = request.get_json(force=True, silent=True)
        logging.info(f"[Fireflies Webhook] client={client_id} received at {datetime.utcnow()}: {json.dumps(payload)}")

        client = get_client_credentials(client_id)
        if not client:
            logging.error(f"[Fireflies Webhook] Unknown client_id: {client_id}")
            return jsonify({"status": "unknown client"}), 200

        event = payload.get("event")
        meeting_id = payload.get("meeting_id")

        if event == "meeting.summarized" and meeting_id and meeting_id != "test_00000000":
            if not claim_meeting_job(client_id, meeting_id):
                logging.info(f"[Fireflies Webhook] client={client_id} meeting={meeting_id} duplicate delivery, skipping")
                return jsonify({"status": "duplicate"}), 200

            thread = threading.Thread(
                target=process_meeting_in_background,
                args=(client_id, client, meeting_id),
                daemon=True
            )
            thread.start()
            logging.info(f"[Fireflies Webhook] client={client_id} meeting={meeting_id} handed off to background thread")

        return jsonify({"status": "received"}), 200

    except Exception as e:
        logging.error(f"[Fireflies Webhook] client={client_id} error: {e}")
        return jsonify({"status": "error logged"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
