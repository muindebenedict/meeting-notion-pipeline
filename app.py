import os
import json
import time
import logging
from datetime import datetime
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


def fetch_fireflies_summary(meeting_id, fireflies_api_key, max_retries=4, initial_delay=8):
    """
    Fetches the transcript summary for a given meeting_id from Fireflies.
    Retries with exponential backoff if Fireflies returns an error or an
    empty summary (common right after the meeting.summarized webhook fires,
    since the summary can still be writing on Fireflies' side).
    """
    query = """
    query Transcript($transcriptId: String!) {
        transcript(id: $transcriptId) {
            title
            date
            summary {
                overview
                action_items
                keywords
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
        try:
            response = ext_requests.post(
                "https://api.fireflies.ai/graphql",
                headers=headers,
                json={"query": query, "variables": {"transcriptId": meeting_id}},
                timeout=15
            )
            response.raise_for_status()
            data = response.json()

            transcript = data.get("data", {}).get("transcript")
            summary = transcript.get("summary") if transcript else None

            if summary and summary.get("overview"):
                logging.info(f"[Fireflies Fetch] meeting={meeting_id} succeeded on attempt {attempt}")
                return transcript

            # Got a response but summary isn't ready yet — retry
            logging.warning(f"[Fireflies Fetch] meeting={meeting_id} summary not ready on attempt {attempt}, retrying in {delay}s")

        except ext_requests.exceptions.RequestException as e:
            last_error = e
            logging.warning(f"[Fireflies Fetch] meeting={meeting_id} error on attempt {attempt}: {e}, retrying in {delay}s")

        if attempt < max_retries:
            time.sleep(delay)
            delay *= 2  # exponential backoff: 8s, 16s, 32s...

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
            meeting_data = fetch_fireflies_summary(meeting_id, client["fireflies_api_key"])
            logging.info(f"[Fireflies Webhook] client={client_id} fetched summary: {json.dumps(meeting_data)}")
            push_to_notion(
                meeting_data,
                meeting_id,
                client["notion_api_key"],
                client["notion_database_id"]
            )
            logging.info(f"[Fireflies Webhook] client={client_id} pushed to Notion successfully")

        return jsonify({"status": "received"}), 200

    except Exception as e:
        logging.error(f"[Fireflies Webhook] client={client_id} error: {e}")
        return jsonify({"status": "error logged"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
