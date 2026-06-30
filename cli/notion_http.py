import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = "https://www.notion.so/api/v3"
MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds


def _make_headers(token_v2: str, user_id: str | None = None, space_id: str | None = None) -> dict:
    cookie = f"token_v2={token_v2}"
    if user_id:
        cookie += f"; notion_user_id={user_id}"
        
    headers = {
        "Content-Type": "application/json",
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:147.0) Gecko/20100101 Firefox/147.0",
        "notion-audit-log-platform": "web",
        "notion-client-version": "23.13.20260307.1532",
        "Origin": "https://www.notion.so",
        "Referer": "https://www.notion.so/",
    }
    if user_id:
        headers["x-notion-active-user-header"] = user_id
    if space_id:
        headers["x-notion-space-id"] = space_id
    return headers


def _post(endpoint: str, payload: dict, token_v2: str, user_id: str | None = None,
          dry_run: bool = False, space_id: str | None = None) -> dict:
    """POST to a Notion internal endpoint with retry on 5xx."""
    url = f"{BASE_URL}/{endpoint}"
    body = json.dumps(payload).encode()

    if dry_run:
        print(f"[DRY RUN] POST {url}")
        print(json.dumps(payload, indent=2))
        return {}

    headers = _make_headers(token_v2, user_id, space_id)
    last_err = None

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data
        except urllib.error.HTTPError as e:
            status = e.code
            body_text = e.read().decode(errors="replace")

            # Token expired — caller should refresh and retry
            if status in (401, 403):
                raise PermissionError(
                    f"Notion returned {status}. token_v2 may be expired. "
                    f"Response: {body_text}"
                ) from e

            # Retryable server errors
            if status >= 500:
                last_err = e
                wait = BACKOFF_BASE * (2 ** attempt)
                print(f"  [{attempt+1}/{MAX_RETRIES}] {status} error, retrying in {wait}s...")
                time.sleep(wait)
                continue

            # Non-retryable client error
            raise RuntimeError(
                f"Notion API error {status}: {body_text}"
            ) from e

        except urllib.error.URLError as e:
            last_err = e
            wait = BACKOFF_BASE * (2 ** attempt)
            print(f"  [{attempt+1}/{MAX_RETRIES}] Network error, retrying in {wait}s: {e}")
            time.sleep(wait)

    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts: {last_err}")


def _post_fire_and_forget(endpoint: str, payload: dict, token_v2: str,
                          user_id: str | None = None,
                          space_id: str | None = None) -> None:
    """POST to a Notion endpoint and return immediately without reading the response.

    Used for streaming endpoints (e.g. runInferenceTranscript) where the response
    is NDJSON that blocks until inference completes. We only need to fire the
    request; the server processes it regardless of whether we consume the stream.
    """
    url = f"{BASE_URL}/{endpoint}"
    body = json.dumps(payload).encode()
    headers = _make_headers(token_v2, user_id, space_id)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        # Read a tiny prefix to confirm the server accepted, then close
        resp.read(64)
        resp.close()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"Inference trigger failed: {e}") from e


def _tx(space_id: str, operations: list[dict], *,
        user_action: str = "cli.update_agent",
        unretryable_error_behavior: str | None = None) -> dict:
    """Wrap operations in the modern saveTransactionsFanout envelope."""
    payload = {
        "requestId": str(uuid.uuid4()),
        "transactions": [{
            "id": str(uuid.uuid4()),
            "spaceId": space_id,
            "debug": {"userAction": user_action},
            "operations": operations,
        }],
    }
    if unretryable_error_behavior:
        payload["unretryable_error_behavior"] = unretryable_error_behavior
    return payload


def _block_pointer(notion_public_id: str, space_id: str) -> dict:
    return {"table": "block", "id": notion_public_id, "spaceId": space_id}


MAX_OPS_PER_TX = 200


def send_ops(space_id: str, ops: list[dict],
             token_v2: str, user_id: str | None = None,
             dry_run: bool = False,
             user_action: str = "cli.update_agent",
             endpoint: str = "saveTransactionsFanout") -> None:
    """Send operations to Notion, batching into multiple transactions if needed.

    Notion's saveTransactionsFanout returns 500 on very large payloads.
    Operations are split into chunks of MAX_OPS_PER_TX and sent sequentially.

    endpoint:
        "saveTransactionsFanout" — distributed write, used for block/content edits.
        "saveTransactionsMain"   — primary-store write, used by the UI for workflow
                                   settings-page edits (model, modules, permissions).
                                   Mirror UI behaviour for any workflow record mutations.
    """
    if not ops:
        return
    for i in range(0, len(ops), MAX_OPS_PER_TX):
        chunk = ops[i:i + MAX_OPS_PER_TX]
        _post(endpoint, _tx(space_id, chunk, user_action=user_action),
              token_v2, user_id, dry_run, space_id=space_id)


def _record_value(entry: dict | None) -> dict:
    """Unwrap recordMap entries, which sometimes nest value.value."""
    if not isinstance(entry, dict):
        return {}
    value = entry.get("value")
    if not isinstance(value, dict):
        return {}
    nested = value.get("value")
    if isinstance(nested, dict) and "id" in nested:
        return nested
    return value


def _normalize_record_map(data: dict) -> dict:
    """Normalize all recordMap tables: unwrap double-nested value.value -> value."""
    for table_records in data.get("recordMap", {}).values():
        if not isinstance(table_records, dict):
            continue
        for rid in table_records:
            entry = table_records[rid]
            if isinstance(entry, dict):
                unwrapped = _record_value(entry)
                if unwrapped:
                    table_records[rid] = {"value": unwrapped}
    return data


def _chunked(items: list[str], size: int = 50) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def read_records(
    table: str,
    ids: list[str],
    token_v2: str,
    user_id: str | None = None,
    *,
    space_id: str | None = None,
) -> dict[str, dict]:
    """Read records by table/id with the correct transport for each table.

    `workflow_artifact` is space-sharded and requires syncRecordValuesSpaceInitial
    with a pointer that includes spaceId. Most other tables work with getRecordValues.
    """
    records: dict[str, dict] = {}
    unique_ids = sorted({item for item in ids if item})
    if not unique_ids:
        return records

    use_space_pointer = table == "workflow_artifact"
    if use_space_pointer and not space_id:
        raise ValueError("space_id is required to read workflow_artifact records")

    for batch in _chunked(unique_ids):
        missing_ids = list(batch)
        try:
            if use_space_pointer:
                payload = {
                    "requests": [
                        {
                            "pointer": {
                                "table": table,
                                "id": item_id,
                                "spaceId": space_id,
                            },
                            "version": -1,
                        }
                        for item_id in batch
                    ]
                }
                data = _normalize_record_map(
                    _post("syncRecordValuesSpaceInitial", payload, token_v2, user_id)
                )
                record_map = (data.get("recordMap", {}) or {}).get(table, {})
                for item_id in batch:
                    value = (record_map.get(item_id) or {}).get("value")
                    if isinstance(value, dict):
                        records[item_id] = value
                missing_ids = [item_id for item_id in batch if item_id not in records]
            else:
                payload = {"requests": [{"id": item_id, "table": table} for item_id in batch]}
                data = _post("getRecordValues", payload, token_v2, user_id, space_id=space_id)
                for item_id, result in zip(batch, data.get("results", [])):
                    value = result.get("value")
                    if isinstance(value, dict):
                        records[item_id] = value
                missing_ids = [item_id for item_id in batch if item_id not in records]
        except Exception:
            missing_ids = list(batch)

        for item_id in missing_ids:
            try:
                if use_space_pointer:
                    payload = {
                        "requests": [
                            {
                                "pointer": {
                                    "table": table,
                                    "id": item_id,
                                    "spaceId": space_id,
                                },
                                "version": -1,
                            }
                        ]
                    }
                    data = _normalize_record_map(
                        _post("syncRecordValuesSpaceInitial", payload, token_v2, user_id)
                    )
                    value = (((data.get("recordMap", {}) or {}).get(table, {}).get(item_id) or {}).get("value"))
                else:
                    payload = {"requests": [{"id": item_id, "table": table}]}
                    data = _post("getRecordValues", payload, token_v2, user_id, space_id=space_id)
                    results = data.get("results", [])
                    value = results[0].get("value") if results else None
                if isinstance(value, dict):
                    records[item_id] = value
            except Exception:
                continue
    return records
