"""
notion_client.py — HTTP client for Notion's internal /api/v3/ endpoints.

Implements:
  - loadPageChunk         → read child block IDs of an instructions page
  - saveTransactionsFanout → write block content (delete old, insert new)
  - publishCustomAgentVersion → deploy agent after instruction update

Transaction envelope format from live capture (2026-03-03) and
notion-enhancer/api/notion.mjs (MIT):
https://github.com/notion-enhancer/api/blob/dev/notion.mjs
"""

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE_URL = "https://www.notion.so/api/v3"
MAX_RETRIES = 3
BACKOFF_BASE = 1  # seconds


def _make_headers(token_v2: str, user_id: str | None = None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"token_v2={token_v2}",
        "Notion-Audit-Log-Platform": "web",
    }
    if user_id:
        headers["x-notion-active-user-header"] = user_id
    return headers


def _post(endpoint: str, payload: dict, token_v2: str, user_id: str | None = None,
          dry_run: bool = False) -> dict:
    """POST to a Notion internal endpoint with retry on 5xx."""
    url = f"{BASE_URL}/{endpoint}"
    body = json.dumps(payload).encode()

    if dry_run:
        print(f"[DRY RUN] POST {url}")
        print(json.dumps(payload, indent=2))
        return {}

    headers = _make_headers(token_v2, user_id)
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


# ── Discover ──────────────────────────────────────────────────────────────────

def get_user_spaces(token_v2: str) -> list[dict]:
    """
    List all spaces (workspaces) the user belongs to.
    Uses loadUserContent which is more reliable than getSpaces.
    """
    data = _post("loadUserContent", {}, token_v2)
    
    record_map = data.get("recordMap", {})
    spaces_map = record_map.get("space", {})
    
    spaces = []
    for space_id, space_rec in spaces_map.items():
        v = space_rec.get("value", {})
        if not v.get("alive", True):
            continue
        spaces.append({
            "id": space_id,
            "name": v.get("name"),
            "domain": v.get("domain"),
        })
    return sorted(spaces, key=lambda s: (s["name"] or "").lower())


def get_all_workspace_agents(space_id: str, token_v2: str,
                              user_id: str | None = None) -> list[dict]:
    """
    Enumerate all AI agents (workflows) in a Notion workspace.

    Uses two calls:
      1. POST /api/v3/getBots — returns bot records keyed by bot_id, each with workflow_id
      2. POST /api/v3/getRecordValues (batched) — fetches all workflow records for name + block_id

    Returns a list of dicts:
      {name, workflow_id, space_id, block_id}
    """
    # Step 1: getBots — list all workflow-type bots in the space
    bots_data = _post("getBots", {"table": "space", "id": space_id, "type": "workflow"},
                      token_v2, user_id)
    bot_records = bots_data.get("recordMap", {}).get("bot", {})

    # Collect unique workflow_ids, keeping highest version per workflow
    seen: dict[str, dict] = {}
    for bot_data in bot_records.values():
        v = bot_data.get("value", {})
        if not v.get("alive", True):
            continue
        wf_id = v.get("workflow_id", "")
        if not wf_id:
            continue
        version = v.get("version", 0)
        name = v.get("name", "")
        if wf_id not in seen or version > seen[wf_id]["version"]:
            seen[wf_id] = {"name": name, "version": version}

    if not seen:
        return []

    workflow_ids = list(seen.keys())

    # Step 2: batch getRecordValues — fetch all workflow records for block_id
    batch_payload = {
        "requests": [{"id": wid, "table": "workflow"} for wid in workflow_ids],
    }
    wf_data = _post("getRecordValues", batch_payload, token_v2, user_id)

    agents = []
    for i, result in enumerate(wf_data.get("results", [])):
        wf = result.get("value")
        if not wf:
            continue
        wf_id = workflow_ids[i]
        data = wf.get("data", {})
        name = data.get("name") or seen[wf_id]["name"]
        instructions = data.get("instructions")
        if not instructions:
            continue
        block_id = instructions["id"] if isinstance(instructions, dict) else instructions
        agents.append({
            "name": name,
            "workflow_id": wf_id,
            "space_id": wf.get("space_id", space_id),
            "block_id": block_id,
            "triggers": data.get("triggers", []),
        })

    return sorted(agents, key=lambda a: a["name"].lower())


def get_workflow_record(workflow_id: str, token_v2: str,
                        user_id: str | None = None) -> dict:
    """
    Fetch a workflow record via getRecordValues (table: "workflow").
    Returns the workflow's value dict with keys like name, data, space_id, etc.
    """
    payload = {
        "requests": [{"id": workflow_id, "table": "workflow"}],
    }
    data = _post("getRecordValues", payload, token_v2, user_id)
    results = data.get("results", [])
    if not results or not results[0].get("value"):
        raise RuntimeError(
            f"Workflow {workflow_id} not found or inaccessible. "
            f"Response: {data}"
        )
    return results[0]["value"]


# ── Read ──────────────────────────────────────────────────────────────────────

def get_block_children(block_id: str, space_id: str,
                       token_v2: str, user_id: str | None = None) -> list[str]:
    """Return ordered list of child block IDs for a given block."""
    payload = {
        "pageId": block_id,
        "limit": 100,
        "cursor": {"stack": []},
        "chunkNumber": 0,
        "verticalColumns": False,
    }
    data = _post("loadPageChunk", payload, token_v2, user_id)

    record_map = data.get("recordMap", {})
    blocks = record_map.get("block", {})

    parent = blocks.get(block_id, {}).get("value", {})
    return parent.get("content", [])


def get_block_tree(block_id: str, space_id: str,
                   token_v2: str, user_id: str | None = None) -> dict:
    """Return the full recordMap for a block and all its descendants, paginating as needed."""
    cursor = {"stack": []}
    merged_blocks: dict = {}
    first_response: dict | None = None

    while True:
        payload = {
            "pageId": block_id,
            "limit": 500,
            "cursor": cursor,
            "chunkNumber": 0,
            "verticalColumns": False,
        }
        data = _post("loadPageChunk", payload, token_v2, user_id)
        if first_response is None:
            first_response = data
        merged_blocks.update(data.get("recordMap", {}).get("block", {}))

        next_cursor = data.get("cursor")
        if not next_cursor or not next_cursor.get("stack"):
            break
        cursor = next_cursor

    first_response.setdefault("recordMap", {})["block"] = merged_blocks
    return first_response


def get_db_automations(db_page_id: str, token_v2: str,
                       user_id: str | None = None) -> dict:
    """
    Return all native automations and their actions for a Notion database page.

    Uses loadPageChunk which includes 'automation' and 'automation_action' tables
    in the recordMap alongside block data.

    Returns:
        {
          "automations": [
            {
              "id": str,
              "enabled": bool | None,
              "trigger": dict,
              "actions": [{"id": str, "type": str, "config": dict}, ...]
            },
            ...
          ]
        }
    """
    payload = {
        "pageId": db_page_id,
        "limit": 100,
        "cursor": {"stack": []},
        "chunkNumber": 0,
        "verticalColumns": False,
    }
    data = _post("loadPageChunk", payload, token_v2, user_id)
    record_map = data.get("recordMap", {})

    raw_automations = record_map.get("automation", {})
    raw_actions = record_map.get("automation_action", {})

    # Build property ID → name map from collection schema
    prop_map: dict[str, str] = {}
    for coll_rec in record_map.get("collection", {}).values():
        schema = coll_rec.get("value", {}).get("schema", {})
        for pid, pdef in schema.items():
            prop_map[pid] = pdef.get("name", pid)

    result = []
    for aid, arec in raw_automations.items():
        av = arec.get("value", {})
        # Collect actions belonging to this automation
        actions = [
            {
                "id": av2.get("id"),
                "type": av2.get("type"),
                "config": av2.get("config", {}),
            }
            for av2 in (v.get("value", {}) for v in raw_actions.values())
            if av2.get("parent_id") == aid
        ]
        result.append({
            "id": aid,
            "enabled": av.get("enabled"),
            "trigger": av.get("trigger"),
            "actions": actions,
        })

    return {"automations": result, "property_map": prop_map}


# ── Write ─────────────────────────────────────────────────────────────────────

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


def _block_pointer(block_id: str, space_id: str) -> dict:
    return {"table": "block", "id": block_id, "spaceId": space_id}


def send_ops(space_id: str, ops: list[dict],
             token_v2: str, user_id: str | None = None,
             dry_run: bool = False,
             user_action: str = "cli.update_agent") -> None:
    """Send a batch of operations in a single transaction."""
    if not ops:
        return
    _post("saveTransactionsFanout", _tx(space_id, ops, user_action=user_action), token_v2, user_id, dry_run)


def _record_value(entry: dict | None) -> dict:
    """Unwrap recordMap entries, which sometimes nest value.value."""
    if not isinstance(entry, dict):
        return {}
    value = entry.get("value")
    if not isinstance(value, dict):
        return {}
    nested = value.get("value")
    if isinstance(nested, dict):
        return nested
    return value


# ── Op collectors (build ops without sending) ────────────────────────────────

def _ops_delete_block(block_id: str, parent_id: str, space_id: str) -> list[dict]:
    """Return ops to soft-delete a block and remove from parent content."""
    return [
        {
            "pointer": _block_pointer(block_id, space_id),
            "path": [],
            "command": "update",
            "args": {"alive": False},
        },
        {
            "pointer": _block_pointer(parent_id, space_id),
            "path": ["content"],
            "command": "listRemove",
            "args": {"id": block_id},
        },
    ]


def _ops_insert_block(block: dict, parent_id: str, after_id: str | None,
                      space_id: str) -> tuple[list[dict], str]:
    """Return (ops, new_block_id) to insert a block. Handles children recursively."""
    children = block.pop("children", None)
    block_id = str(uuid.uuid4())
    now = int(time.time() * 1000)

    block_value = {
        "id": block_id,
        "parent_id": parent_id,
        "parent_table": "block",
        "alive": True,
        "created_time": now,
        "last_edited_time": now,
        "space_id": space_id,
        **block,
    }

    list_after_args: dict[str, Any] = {"id": block_id}
    if after_id:
        list_after_args["after"] = after_id

    ops = [
        {
            "pointer": _block_pointer(block_id, space_id),
            "path": [],
            "command": "set",
            "args": block_value,
        },
        {
            "pointer": _block_pointer(parent_id, space_id),
            "path": ["content"],
            "command": "listAfter",
            "args": list_after_args,
        },
    ]

    if children:
        child_after = None
        for child_block in children:
            child_ops, child_id = _ops_insert_block(
                child_block, block_id, child_after, space_id,
            )
            ops.extend(child_ops)
            child_after = child_id

    return ops, block_id


def _ops_update_block(block_id: str, space_id: str,
                      properties: dict, format_: dict | None = None) -> list[dict]:
    """Return ops to update a block's properties (and optionally format) in place."""
    ops = [
        {
            "pointer": _block_pointer(block_id, space_id),
            "path": ["properties"],
            "command": "set",
            "args": properties,
        },
    ]
    if format_ is not None:
        ops.append({
            "pointer": _block_pointer(block_id, space_id),
            "path": ["format"],
            "command": "set",
            "args": format_,
        })
    return ops


# ── Legacy single-call wrappers ──────────────────────────────────────────────

def delete_block(block_id: str, parent_id: str, space_id: str,
                 token_v2: str, user_id: str | None = None,
                 dry_run: bool = False) -> None:
    """Soft-delete a block and remove it from its parent's content list."""
    send_ops(space_id, _ops_delete_block(block_id, parent_id, space_id),
             token_v2, user_id, dry_run)


def insert_block(block: dict, parent_id: str, after_id: str | None,
                 space_id: str, token_v2: str, user_id: str | None = None,
                 dry_run: bool = False) -> str:
    """Insert a new block into parent_id after after_id (or at start if None).
    Returns the new block's ID."""
    ops, block_id = _ops_insert_block(block, parent_id, after_id, space_id)
    send_ops(space_id, ops, token_v2, user_id, dry_run)
    return block_id


# ── Block fingerprinting ─────────────────────────────────────────────────────

def _title_text(block: dict) -> str:
    """Extract plain text from a block's title property."""
    title = block.get("properties", {}).get("title", [])
    if not title:
        return ""
    return "".join(chunk[0] for chunk in title if chunk)


def _block_fingerprint(block: dict) -> tuple:
    """Canonical fingerprint for comparing blocks regardless of source.

    Works on both API block records (have id, parent_id, etc.) and
    builder block dicts (just type + properties). Returns a tuple of
    (type, title_text, language, format_icon, child_fingerprints...).
    """
    btype = block.get("type", "text")
    props = block.get("properties", {})
    title = _title_text(block)
    lang = ""
    if "language" in props:
        lang_val = props["language"]
        lang = lang_val[0][0] if lang_val and lang_val[0] else ""
    fmt = block.get("format", {}).get("page_icon", "")

    # Children: from builder blocks use "children" key,
    # from API blocks we won't recurse here (handled by diff_replace).
    child_fps = ()
    children = block.get("children")
    if children:
        child_fps = tuple(_block_fingerprint(c) for c in children)

    return (btype, title, lang, fmt, child_fps)


def _api_block_fingerprint(block: dict, blocks_map: dict) -> tuple:
    """Fingerprint an API block record, recursing into children via blocks_map."""
    btype = block.get("type", "text")
    props = block.get("properties", {})
    title = _title_text(block)
    lang = ""
    if "language" in props:
        lang_val = props["language"]
        lang = lang_val[0][0] if lang_val and lang_val[0] else ""
    fmt = block.get("format", {}).get("page_icon", "")

    child_fps = ()
    child_ids = block.get("content", [])
    if child_ids:
        child_fps = tuple(
            _api_block_fingerprint(
                blocks_map.get(cid, {}).get("value", {}), blocks_map
            )
            for cid in child_ids
            if blocks_map.get(cid, {}).get("value", {}).get("alive", True)
        )

    return (btype, title, lang, fmt, child_fps)


# ── Diff-based replace ───────────────────────────────────────────────────────

def _collect_delete_tree_ops(block_id: str, parent_id: str, space_id: str,
                             blocks_map: dict) -> list[dict]:
    """Collect ops to delete a block and all its descendants."""
    ops = []
    block = blocks_map.get(block_id, {}).get("value", {})
    for child_id in block.get("content", []):
        ops.extend(_collect_delete_tree_ops(child_id, block_id, space_id, blocks_map))
    ops.extend(_ops_delete_block(block_id, parent_id, space_id))
    return ops


def diff_replace_block_content(
    parent_id: str, space_id: str,
    new_blocks: list[dict],
    token_v2: str, user_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Replace block content with minimal operations using a structural diff.

    Compares existing blocks to new blocks by fingerprint. Finds the longest
    matching prefix and suffix, then only touches the changed zone in the middle.
    Within the changed zone, blocks with matching types get in-place property
    updates instead of delete+insert cycles.

    Returns a stats dict: {unchanged, deleted, inserted, updated, ops, api_calls_saved}.
    """
    # 1. Read existing block tree (single API call)
    tree = get_block_tree(parent_id, space_id, token_v2, user_id)
    blocks_map = tree.get("recordMap", {}).get("block", {})

    parent_block = blocks_map.get(parent_id, {}).get("value", {})
    existing_ids = parent_block.get("content", [])

    # Build fingerprints for existing blocks
    existing_fps = []
    for bid in existing_ids:
        bdata = blocks_map.get(bid, {}).get("value", {})
        if not bdata or not bdata.get("alive", True):
            continue
        existing_fps.append((bid, _api_block_fingerprint(bdata, blocks_map)))

    # Build fingerprints for new blocks
    new_fps = [_block_fingerprint(b) for b in new_blocks]

    # 2. Find longest matching prefix
    prefix_len = 0
    for i in range(min(len(existing_fps), len(new_fps))):
        if existing_fps[i][1] == new_fps[i]:
            prefix_len = i + 1
        else:
            break

    # 3. Find longest matching suffix (after prefix)
    suffix_len = 0
    max_suffix = min(len(existing_fps) - prefix_len, len(new_fps) - prefix_len)
    for i in range(1, max_suffix + 1):
        if existing_fps[-i][1] == new_fps[-i]:
            suffix_len = i
        else:
            break

    # 4. Determine changed zone boundaries
    old_start = prefix_len
    old_end = len(existing_fps) - suffix_len
    new_start = prefix_len
    new_end = len(new_fps) - suffix_len

    n_unchanged = prefix_len + suffix_len
    n_deleted = 0
    n_inserted = 0
    n_updated = 0

    # 5. Nothing changed — no-op
    if old_start >= old_end and new_start >= new_end:
        return {
            "unchanged": n_unchanged, "deleted": 0, "inserted": 0,
            "updated": 0, "ops": 0, "api_calls_saved": len(existing_fps),
        }

    ops: list[dict] = []

    # 6. Within the changed zone, try in-place updates where types match
    old_zone = existing_fps[old_start:old_end]
    new_zone = new_blocks[new_start:new_end]

    # Walk both zones: update in place when type matches, else delete old / insert new
    i_old, i_new = 0, 0
    # Track which old blocks we've consumed (for delete)
    consumed_old: set[int] = set()

    while i_old < len(old_zone) and i_new < len(new_zone):
        old_bid, old_fp = old_zone[i_old]
        new_fp = new_fps[new_start + i_new]
        new_block = new_zone[i_new]

        if old_fp == new_fp:
            # Identical — skip
            n_unchanged += 1
            consumed_old.add(i_old)
            i_old += 1
            i_new += 1
        elif old_fp[0] == new_fp[0]:
            # Same type, different content — update in place
            new_props = new_block.get("properties", {})
            new_fmt = new_block.get("format")
            ops.extend(_ops_update_block(old_bid, space_id, new_props, new_fmt))

            # Handle children: if new block has children, replace them
            new_children = new_block.get("children")
            old_block = blocks_map.get(old_bid, {}).get("value", {})
            old_child_ids = old_block.get("content", [])

            if new_children or old_child_ids:
                # Delete all old children
                for cid in old_child_ids:
                    ops.extend(_collect_delete_tree_ops(cid, old_bid, space_id, blocks_map))
                # Insert new children
                if new_children:
                    child_after = None
                    for child_block in new_children:
                        child_ops, child_id = _ops_insert_block(
                            child_block, old_bid, child_after, space_id,
                        )
                        ops.extend(child_ops)
                        child_after = child_id

            n_updated += 1
            consumed_old.add(i_old)
            i_old += 1
            i_new += 1
        else:
            # Type mismatch — delete old block, advance old pointer
            ops.extend(_collect_delete_tree_ops(old_bid, parent_id, space_id, blocks_map))
            consumed_old.add(i_old)
            n_deleted += 1
            i_old += 1

    # Delete remaining unconsumed old blocks
    while i_old < len(old_zone):
        old_bid, _ = old_zone[i_old]
        if i_old not in consumed_old:
            ops.extend(_collect_delete_tree_ops(old_bid, parent_id, space_id, blocks_map))
            n_deleted += 1
        i_old += 1

    # Insert remaining new blocks
    if i_new < len(new_zone):
        # Insertion point: after the last block before the changed zone,
        # or after the last consumed/updated old block
        if consumed_old:
            max_consumed = max(consumed_old)
            # Use the last old block that was updated (not deleted) as anchor
            after_id = old_zone[max_consumed][0]
            # But if that block was deleted, walk back
            while max_consumed in consumed_old and max_consumed >= 0:
                old_bid_check = old_zone[max_consumed][0]
                # Check if we deleted it (it'll have alive=False in our ops)
                was_deleted = any(
                    op.get("args", {}).get("alive") is False
                    and op.get("pointer", {}).get("id") == old_bid_check
                    for op in ops
                )
                if not was_deleted:
                    after_id = old_bid_check
                    break
                max_consumed -= 1
            else:
                # All old blocks in zone were deleted; anchor to prefix
                after_id = existing_fps[prefix_len - 1][0] if prefix_len > 0 else None
        else:
            after_id = existing_fps[prefix_len - 1][0] if prefix_len > 0 else None

        while i_new < len(new_zone):
            insert_ops, new_id = _ops_insert_block(
                new_zone[i_new], parent_id, after_id, space_id,
            )
            ops.extend(insert_ops)
            after_id = new_id
            n_inserted += 1
            i_new += 1

    # 7. Send all ops in a single transaction
    send_ops(space_id, ops, token_v2, user_id, dry_run)

    # How many API calls the old approach would have made
    old_approach_calls = len(existing_ids) + len(new_blocks)

    return {
        "unchanged": n_unchanged,
        "deleted": n_deleted,
        "inserted": n_inserted,
        "updated": n_updated,
        "ops": len(ops),
        "api_calls_saved": max(0, old_approach_calls - 1),
    }


def replace_block_content(parent_id: str, space_id: str,
                          new_blocks: list[dict],
                          token_v2: str, user_id: str | None = None,
                          dry_run: bool = False) -> None:
    """Replace all children of parent_id with new_blocks.
    Uses batched transaction — single API call regardless of block count."""
    existing = get_block_children(parent_id, space_id, token_v2, user_id)

    ops: list[dict] = []
    for child_id in existing:
        ops.extend(_ops_delete_block(child_id, parent_id, space_id))

    after_id = None
    for block in new_blocks:
        insert_ops, after_id = _ops_insert_block(block, parent_id, after_id, space_id)
        ops.extend(insert_ops)

    send_ops(space_id, ops, token_v2, user_id, dry_run)


# ── Conversations ─────────────────────────────────────────────────────────────

def _extract_rich_text(value) -> str | None:
    """Port of extractRichText from service-worker.js."""
    if not isinstance(value, list):
        return str(value).strip() if value else None
    parts = []
    for chunk in value:
        if not isinstance(chunk, list):
            parts.append(str(chunk) if chunk else "")
            continue
        text = chunk[0] if chunk else ""
        ann = chunk[1] if len(chunk) > 1 else None
        if text == "\u2023" and isinstance(ann, list):  # ‣ mention
            for a in ann:
                if isinstance(a, list) and len(a) >= 2:
                    parts.append(f"[{a[0]}:{a[1]}]")
                    break
        else:
            parts.append(text or "")
    return "".join(parts).strip() or None


def _clean_text(text: str) -> str:
    """Port of cleanText from service-worker.js."""
    import re as _re
    text = _re.sub(r'<lang[^>]*/>', '', text)
    text = _re.sub(r'<edit_reference[^>]*>[\s\S]*?</edit_reference>', '', text)
    return text.strip()


def _extract_inference_turn(step: dict) -> dict | None:
    """Extract an assistant turn from an agent-inference step.

    Captures text, thinking, and tool_use values. Tool uses are stored
    as inline toolCalls with id + name + input so that subsequent
    agent-tool-result messages can attach their results via toolCallId.
    """
    resp, think, tool_calls = [], None, []
    for v in step.get("value") or []:
        if v.get("type") == "text":
            c = _clean_text(v.get("content") or "")
            if c:
                resp.append(c)
        elif v.get("type") == "thinking":
            t = (v.get("content") or "").strip()
            if t:
                think = t
        elif v.get("type") == "tool_use":
            tc: dict = {"tool": v.get("name") or "unknown_tool"}
            if v.get("id"):
                tc["toolCallId"] = v["id"]
            raw_content = v.get("content")
            if raw_content:
                try:
                    tc["input"] = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                except (json.JSONDecodeError, TypeError):
                    tc["input"] = raw_content
            tool_calls.append(tc)
    if not resp and not tool_calls:
        return None
    content = "\n".join(resp) if resp else ""
    turn: dict = {"role": "assistant", "content": content}
    if think:
        turn["thinking"] = think
    if tool_calls:
        turn["toolCalls"] = tool_calls
    if step.get("model"):
        turn["model"] = step["model"]
    return turn


def get_thread_conversation(thread_id: str, token_v2: str,
                             user_id: str | None = None) -> dict:
    """
    Fetch a Notion AI thread and all its messages, returning a parsed
    conversation dict matching the extension's export shape.

    Steps:
      1. getRecordValues(table="thread") → message order + title
      2. getRecordValues(table="thread_message") batched → parse turns + tool calls
    """
    # Step 1: thread record
    thread_resp = _post(
        "getRecordValues",
        {"requests": [{"id": thread_id, "table": "thread"}]},
        token_v2, user_id,
    )
    results = thread_resp.get("results", [])
    if not results:
        raise ValueError(f"Thread '{thread_id}' not found.")
    rec = results[0]
    if not rec.get("value"):
        if rec.get("role"):
            raise ValueError(
                f"Thread '{thread_id}' exists but its content has been deleted or purged by Notion. "
                "This happens with old or cleared conversations."
            )
        raise ValueError(f"Thread '{thread_id}' not found or inaccessible.")
    thread = results[0]["value"]

    message_ids: list[str] = thread.get("messages") or []
    title: str | None = thread.get("data", {}).get("title") or None
    space_id: str = thread.get("space_id", "")

    if not message_ids:
        return {
            "id": f"thread-{thread_id.replace('-', '')}",
            "threadId": thread_id, "spaceId": space_id, "title": title,
            "turns": [], "toolCalls": [],
            "createdAt": thread.get("created_time"),
            "updatedAt": thread.get("updated_time"),
            "createdById": thread.get("created_by_id"),
            "updatedById": thread.get("updated_by_id"),
        }

    # Step 2: batch fetch messages (in order)
    msg_resp = _post(
        "getRecordValues",
        {"requests": [{"id": mid, "table": "thread_message"} for mid in message_ids]},
        token_v2, user_id,
    )

    turns: list[dict] = []
    orphan_tool_calls: list[dict] = []

    for i, result in enumerate(msg_resp.get("results", [])):
        msg = result.get("value")
        if not msg:
            continue
        mid = message_ids[i]
        step = msg.get("step") or {}
        ts = msg.get("created_time")
        author = msg.get("created_by_id")

        if step.get("type") == "agent-inference":
            turn = _extract_inference_turn(step)
            if turn:
                turn["msgId"] = mid
                if ts:
                    turn["timestamp"] = ts
                if author:
                    turn["createdById"] = author
                turns.append(turn)

        elif step.get("type") in ("user", "human"):
            content = _extract_rich_text(step.get("value"))
            if content:
                turn_data = {"role": "user", "content": content,
                             "msgId": mid, "timestamp": ts}
                if author:
                    turn_data["createdById"] = author
                turns.append(turn_data)

        elif (step.get("type") == "agent-tool-result"
              and step.get("state") == "applied"
              and step.get("toolName")):
            result_data = step.get("result")
            tool_call_id = step.get("toolCallId")
            agent_step_id = step.get("agentStepId")

            # Try to merge into an existing inline toolCall via toolCallId
            merged = False
            if agent_step_id and tool_call_id:
                parent_idx = next(
                    (j for j, t in enumerate(turns) if t.get("msgId") == agent_step_id),
                    -1,
                )
                if parent_idx >= 0:
                    for tc in turns[parent_idx].get("toolCalls", []):
                        if tc.get("toolCallId") == tool_call_id:
                            tc["result"] = result_data
                            if not tc.get("input") and step.get("input"):
                                tc["input"] = step["input"]
                            merged = True
                            break

            if not merged:
                tool_call = {
                    "tool": step["toolName"],
                    "input": step.get("input") or {},
                    "result": result_data,
                }
                if tool_call_id:
                    tool_call["toolCallId"] = tool_call_id
                # Fall back to parent matching by agentStepId
                parent_idx = next(
                    (j for j, t in enumerate(turns) if t.get("msgId") == agent_step_id),
                    -1,
                ) if agent_step_id else -1
                if parent_idx >= 0:
                    turns[parent_idx].setdefault("toolCalls", []).append(tool_call)
                else:
                    orphan_tool_calls.append(tool_call)

    # Derive model from first assistant turn that has one
    model = next((t.get("model") for t in turns if t.get("model")), None)

    return {
        "id": f"thread-{thread_id.replace('-', '')}",
        "threadId": thread_id,
        "spaceId": space_id,
        "title": title,
        "model": model,
        "turns": turns,
        "toolCalls": orphan_tool_calls,
        "createdAt": thread.get("created_time"),
        "updatedAt": thread.get("updated_time"),
        "createdById": thread.get("created_by_id"),
        "updatedById": thread.get("updated_by_id"),
    }


def search_threads(query: str, space_id: str, token_v2: str,
                   user_id: str | None = None) -> list[dict]:
    """
    Search for Notion AI threads by title using the internal search endpoint.
    Returns list of {thread_id, title, created_time} dicts.
    """
    payload = {
        "type": "BlocksInSpace",
        "query": query,
        "spaceId": space_id,
        "filters": {
            "isDeletedOnly": False,
            "excludeTemplates": False,
            "isNavigableOnly": False,
            "requireEditPermissions": False,
        },
        "sort": "Relevance",
        "limit": 20,
    }
    data = _post("search", payload, token_v2, user_id)
    record_map = data.get("recordMap", {})
    thread_rm = record_map.get("thread", {})
    matches = []
    for result in data.get("results", []):
        if result.get("table") == "thread":
            tid = result.get("id", "")
            rec = (thread_rm.get(tid) or {}).get("value", {})
            matches.append({
                "thread_id": tid,
                "title": rec.get("data", {}).get("title") or "(no title)",
                "created_time": rec.get("created_time"),
            })
    return matches


def list_workflow_threads(workflow_id: str, space_id: str,
                          token_v2: str, user_id: str | None = None,
                          limit: int = 100) -> list[dict]:
    """
    List conversation threads for a workflow via getInferenceTranscriptsForWorkflow.

    Endpoint and cursor shape verified from HAR capture (2026-03-06).
    Returns newest-first thread metadata with keys like:
      {id, title, created_at, updated_at, created_by_display_name, trigger_id, run_id, type}
    """
    threads: list[dict] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor: str | None = None

    while True:
        payload = {
            "workflowId": workflow_id,
            "spaceId": space_id,
            "limit": limit,
        }
        if user_id:
            payload["userId"] = user_id
        if cursor:
            payload["cursor"] = cursor

        data = _post("getInferenceTranscriptsForWorkflow", payload, token_v2, user_id)
        transcripts = data.get("transcripts") or []
        transcript_by_id = {
            item.get("id"): item for item in transcripts
            if isinstance(item, dict) and item.get("id")
        }
        record_threads = (data.get("recordMap") or {}).get("thread") or {}

        raw_ids = data.get("threadIds") or list(transcript_by_id.keys())
        for thread_id in raw_ids:
            if not thread_id or thread_id in seen_ids:
                continue

            transcript = dict(transcript_by_id.get(thread_id) or {})
            record = _record_value(record_threads.get(thread_id))
            if record and record.get("alive") is False:
                continue

            record_data = record.get("data") or {}
            meta = {
                "id": thread_id,
                "title": transcript.get("title") or record_data.get("title"),
                "created_at": transcript.get("created_at") or record.get("created_time"),
                "updated_at": transcript.get("updated_at") or record.get("updated_time"),
                "created_by_display_name": transcript.get("created_by_display_name"),
                "trigger_id": transcript.get("trigger_id") or record_data.get("trigger_id"),
                "run_id": transcript.get("run_id") or record_data.get("run_id"),
                "type": transcript.get("type") or record.get("type") or "workflow",
            }
            threads.append({k: v for k, v in meta.items() if v is not None})
            seen_ids.add(thread_id)

        next_cursor = data.get("nextCursor")
        if not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return threads


def archive_threads(thread_ids: list[str], space_id: str,
                    token_v2: str, user_id: str | None = None,
                    dry_run: bool = False) -> list[str]:
    """Soft-delete one or more thread records using the UI's captured payload shape."""
    seen: set[str] = set()
    ordered_ids = [
        thread_id for thread_id in thread_ids
        if thread_id and not (thread_id in seen or seen.add(thread_id))
    ]
    if not ordered_ids:
        return []

    ops = [{
        "pointer": {"table": "thread", "id": thread_id, "spaceId": space_id},
        "path": [],
        "command": "update",
        "args": {
            "alive": False,
            "current_inference_id": None,
            "current_inference_lease_expiration": None,
        },
    } for thread_id in ordered_ids]

    payload = _tx(
        space_id,
        ops,
        user_action="assistantChatHistoryItem.deleteInferenceChatTranscript",
        unretryable_error_behavior="continue",
    )
    _post("saveTransactionsFanout", payload, token_v2, user_id, dry_run)
    return ordered_ids


def archive_workflow_threads(workflow_id: str, space_id: str,
                             token_v2: str, user_id: str | None = None,
                             limit: int = 100) -> dict:
    """
    Discover and archive manually-created threads for a workflow.

    Threads created by automated triggers (property-change, schedule) carry a
    trigger_id. Deleting those threads appears to break the backend subscription
    that routes future trigger events — the trigger fires but silently drops.
    Only archive threads with no trigger_id (New Chat / @mention sessions).
    """
    threads = list_workflow_threads(workflow_id, space_id, token_v2, user_id, limit=limit)
    manual_ids = [
        thread["id"] for thread in threads
        if thread.get("id") and not thread.get("trigger_id")
    ]
    archived_ids = archive_threads(manual_ids, space_id, token_v2, user_id)
    return {
        "count": len(archived_ids),
        "threadIds": archived_ids,
        "threads": threads,
        "skippedTriggerThreads": len(threads) - len(manual_ids),
    }


# ── Agent tools/modules ───────────────────────────────────────────────────────

# Model codename → display name mapping (discovered from live data)
MODEL_NAMES = {
    "avocado-froyo-medium": "Opus 4.6",
    "almond-croissant-low": "Sonnet 4.6",
    "oatmeal-cookie": "ChatGPT (o-series)",
    "oval-kumquat-medium": "ChatGPT 5.4",
    "fireworks-minimax-m2.5": "Minimax M2.5",
    "auto": "Auto",
    "unknown": "Auto (default)",
}


def _resolve_page_names(block_ids: list[str], token_v2: str,
                        user_id: str | None = None) -> dict[str, str]:
    """Batch-resolve block IDs to page titles (handles collection_view_page)."""
    if not block_ids:
        return {}
    payload = {
        "requests": [{"id": bid, "table": "block"} for bid in block_ids],
    }
    data = _post("getRecordValues", payload, token_v2, user_id)
    names = {}
    coll_ids_to_resolve: dict[str, str] = {}  # collection_id -> block_id

    for i, result in enumerate(data.get("results", [])):
        val = result.get("value", {})
        title_prop = val.get("properties", {}).get("title", [])
        if title_prop:
            names[block_ids[i]] = "".join(c[0] for c in title_prop if c)
        else:
            coll_id = val.get("collection_id")
            if coll_id:
                coll_ids_to_resolve[coll_id] = block_ids[i]
            else:
                names[block_ids[i]] = block_ids[i]

    # Resolve collection_view_page names from collection records
    if coll_ids_to_resolve:
        coll_payload = {
            "requests": [{"id": cid, "table": "collection"} for cid in coll_ids_to_resolve],
        }
        coll_data = _post("getRecordValues", coll_payload, token_v2, user_id)
        for i, result in enumerate(coll_data.get("results", [])):
            cid = list(coll_ids_to_resolve.keys())[i]
            bid = coll_ids_to_resolve[cid]
            coll_val = result.get("value", {})
            coll_name = coll_val.get("name", [[""]])[0][0] if coll_val.get("name") else cid
            names[bid] = coll_name

    return names


def get_agent_modules(workflow_id: str, token_v2: str,
                      user_id: str | None = None,
                      resolve_names: bool = True) -> dict:
    """
    Fetch a Notion AI agent's tool/module configuration.

    Returns:
        {
          "model": {"type": "avocado-froyo-medium", "display": "Opus 4.6"},
          "modules": [
            {
              "id": str, "name": str, "type": str,
              ... type-specific fields ...
            }, ...
          ]
        }
    """
    wf = get_workflow_record(workflow_id, token_v2, user_id)
    data = wf.get("data", {})

    model_raw = data.get("model") or {}
    model_type = model_raw.get("type") or "auto"

    modules_raw = data.get("modules", [])
    modules = []

    # Collect block IDs that need name resolution
    block_ids_to_resolve: list[str] = []

    for m in modules_raw:
        mod: dict = {
            "id": m.get("id"),
            "name": m.get("name"),
            "type": m.get("type"),
        }

        if m.get("type") == "notion":
            perms = []
            for p in m.get("permissions", []):
                ident = p.get("identifier", {})
                perm = {
                    "actions": p.get("actions", []),
                    "scope": ident.get("type"),
                }
                if ident.get("blockId"):
                    perm["blockId"] = ident["blockId"]
                    block_ids_to_resolve.append(ident["blockId"])
                perms.append(perm)
            mod["permissions"] = perms

        elif m.get("type") == "mcpServer":
            state = m.get("state", {})
            mod["serverUrl"] = state.get("serverUrl")
            mod["officialName"] = state.get("officialName")
            mod["preferredTransport"] = state.get("preferredTransport")
            mod["runWriteToolsAutomatically"] = state.get("runWriteToolsAutomatically")
            enabled = state.get("enabledToolNames", [])
            all_tools = state.get("tools", [])
            mod["enabledToolNames"] = enabled
            mod["totalTools"] = len(all_tools)
            mod["tools"] = [
                {"name": t["name"], "title": t.get("title", t["name"])}
                for t in all_tools
            ]
            if state.get("connectionPointer"):
                mod["connectionId"] = state["connectionPointer"].get("id")

        elif m.get("type") == "mail":
            state = m.get("state", {})
            mod["scopes"] = state.get("scopes", [])
            addrs = state.get("emailAddresses", [])
            mod["emailAddresses"] = [a.get("email") for a in addrs]

        elif m.get("type") == "calendar":
            state = m.get("state", {})
            mod["scopes"] = state.get("scopes", [])

        modules.append(mod)

    # Resolve block IDs to page names
    if resolve_names and block_ids_to_resolve:
        names = _resolve_page_names(block_ids_to_resolve, token_v2, user_id)
        for mod in modules:
            for perm in mod.get("permissions", []):
                bid = perm.get("blockId")
                if bid and bid in names:
                    perm["pageName"] = names[bid]

    return {
        "model": {"type": model_type, "display": MODEL_NAMES.get(model_type, model_type)},
        "modules": modules,
    }


def update_agent_modules(workflow_id: str, space_id: str,
                         modules: list[dict],
                         token_v2: str, user_id: str | None = None) -> None:
    """
    Update a Notion AI agent's modules array via saveTransactionsFanout.

    modules: The full modules array in Notion's internal format.
    Use get_workflow_record to read the current state, modify, then pass here.
    """
    ops = [{
        "pointer": {"table": "workflow", "id": workflow_id, "spaceId": space_id},
        "path": ["data", "modules"],
        "command": "set",
        "args": modules,
    }]
    send_ops(space_id, ops, token_v2, user_id, user_action="WorkflowActions.saveModule")


def grant_agent_resource_access(workflow_id: str, space_id: str,
                                block_id: str, role: str,
                                token_v2: str, user_id: str | None = None) -> dict:
    """
    Authoritatively grant an agent access to a specific Notion resource.
    Matches the UI's 'NotionModulePermissions.restoreResourceAccess' flow:
      1. Update the 'modules' array on the workflow (UI intent).
      2. Send 'setPermissionItem' on the target block for both Runtime and Draft bots.
      3. Publish the agent to synchronize.
    """
    # 1. Fetch current state to get bots and update modules array
    wf = get_workflow_record(workflow_id, token_v2, user_id)
    modules = wf.get("data", {}).get("modules", [])
    
    runtime_bot = wf.get("data", {}).get("runtime_actor_pointer", {}).get("id")
    draft_bot = wf.get("data", {}).get("draft_runtime_actor_pointer", {}).get("id")
    
    if not runtime_bot:
        raise ValueError(f"Agent {workflow_id} has no runtime_actor_pointer (bot). Publish it first.")

    # Find the Notion module
    notion_mod = next((m for m in modules if m["type"] == "notion"), None)
    if not notion_mod:
        notion_mod = {
            "id": str(uuid.uuid4()),
            "name": "Notion",
            "type": "notion",
            "version": "1.0.0",
            "permissions": []
        }
        modules.append(notion_mod)

    # Update/Append permission intent in modules array
    perms = notion_mod.get("permissions", [])
    found = False
    
    # UI actions: 'read_and_write' or 'reader'
    api_actions = ["read_and_write"] if role in ["editor", "read_and_write"] else ["reader"]
    
    for p in perms:
        if p.get("identifier", {}).get("blockId") == block_id:
            p["actions"] = api_actions
            found = True
            break
            
    if not found:
        perms.append({
            "type": "notion",
            "moduleType": "notion",
            "actions": api_actions,
            "identifier": {
                "type": "pageOrCollectionViewBlock",
                "blockId": block_id
            }
        })
    
    notion_mod["permissions"] = perms

    # 2. Prepare the real authorization operations (setPermissionItem)
    # The role object for 'editor' level access
    role_obj = {"read_content": True, "read_comment": True}
    if "read_and_write" in api_actions:
        role_obj.update({"insert_content": True, "update_content": True, "insert_comment": True})

    auth_ops = []
    for bot_id in filter(None, [runtime_bot, draft_bot]):
        auth_ops.append({
            "pointer": {"table": "block", "id": block_id, "spaceId": space_id},
            "command": "setPermissionItem",
            "path": ["permissions"],
            "args": {
                "type": "bot_permission",
                "bot_id": bot_id,
                "role": role_obj,
                "access_revoked": False
            }
        })

    # 3. Execute all: Update modules (Intent) + Grant (Authorization)
    update_agent_modules(workflow_id, space_id, modules, token_v2, user_id)
    send_ops(space_id, auth_ops, token_v2, user_id, user_action="NotionModulePermissions.restoreResourceAccess")

    # 4. Final Publish Handshake
    return publish_agent(workflow_id, space_id, token_v2, user_id)


def update_agent_model(workflow_id: str, space_id: str,
                       model_type: str,
                       token_v2: str, user_id: str | None = None) -> None:
    """Update a Notion AI agent's model selection."""
    ops = [{
        "pointer": {"table": "workflow", "id": workflow_id, "spaceId": space_id},
        "path": ["data", "model"],
        "command": "set",
        "args": {"type": model_type},
    }]
    send_ops(space_id, ops, token_v2, user_id)


def create_agent(space_id: str, name: str, icon: str | None,
                 token_v2: str, user_id: str) -> dict:
    """
    Create a new Notion AI Agent from scratch.

    This performs Transaction 1:
      - Create the 'workflow' record
      - Create the 'page' block for instructions
      - Create the initial 'text' block inside the instructions page
      - Link them together

    Returns {workflow_id, block_id} on success.
    """
    workflow_id = str(uuid.uuid4())
    instruction_block_id = str(uuid.uuid4())
    initial_text_block_id = str(uuid.uuid4())
    now = int(time.time() * 1000)

    # 1. Workflow record
    wf_args = {
        "id": workflow_id,
        "version": 1,
        "parent_id": space_id,
        "parent_table": "space",
        "space_id": space_id,
        "data": {
            "scripts": [],
            "modules": [{
                "id": str(uuid.uuid4()),
                "type": "notion",
                "name": "Notion",
                "version": "1.0.0",
                "permissions": []
            }],
            "triggers": [{
                "id": str(uuid.uuid4()),
                "moduleId": "", # filled later or left empty for default @mention
                "enabled": True,
                "state": {"type": "notion.agent.mentioned"}
            }],
            "name": name,
            "icon": icon or "https://www.notion.so/images/customAgentAvatars/rock-blue.png",
            "instructions": {
                "table": "block",
                "id": instruction_block_id,
                "spaceId": space_id
            }
        },
        "created_by_table": "notion_user",
        "created_by_id": user_id,
        "created_time": now,
        "last_edited_by_table": "notion_user",
        "last_edited_by_id": user_id,
        "last_edited_time": now,
        "alive": True,
        "permissions": [{
            "type": "user_permission",
            "role": "editor",
            "user_id": user_id
        }]
    }

    # 2. Instruction block (page)
    # Notion uses a complex crdt_data for initial empty title, but a simple set works
    instr_args = {
        "id": instruction_block_id,
        "type": "page",
        "properties": {"title": [["Instructions"]]},
        "space_id": space_id,
        "parent_id": workflow_id,
        "parent_table": "workflow",
        "alive": True,
        "created_time": now,
        "created_by_table": "notion_user",
        "created_by_id": user_id,
        "last_edited_time": now,
        "last_edited_by_id": user_id,
        "last_edited_by_table": "notion_user"
    }

    # 3. Initial text block
    text_args = {
        "id": initial_text_block_id,
        "type": "text",
        "properties": {"title": [["Get started..."]]},
        "space_id": space_id,
        "parent_id": instruction_block_id,
        "parent_table": "block",
        "alive": True,
        "created_time": now,
        "created_by_table": "notion_user",
        "created_by_id": user_id,
        "last_edited_time": now,
        "last_edited_by_id": user_id,
        "last_edited_by_table": "notion_user"
    }

    ops = [
        {"pointer": {"table": "workflow", "id": workflow_id, "spaceId": space_id}, "command": "set", "path": [], "args": wf_args},
        {"pointer": _block_pointer(instruction_block_id, space_id), "command": "set", "path": [], "args": instr_args},
        {"pointer": _block_pointer(initial_text_block_id, space_id), "command": "set", "path": [], "args": text_args},
        {"pointer": _block_pointer(instruction_block_id, space_id), "command": "listAfter", "path": ["content"], "args": {"after": None, "id": initial_text_block_id}}
    ]

    send_ops(space_id, ops, token_v2, user_id, user_action="agentActions.createBlankAgent")

    return {
        "workflow_id": workflow_id,
        "block_id": instruction_block_id
    }


def add_agent_to_sidebar(space_id: str, workflow_id: str,
                         token_v2: str, user_id: str) -> None:
    """
    Append a workflow ID to the user's sidebar (space_view.settings.sidebar_workflow_ids).
    """
    # 1. Find space_view ID
    data = _post("loadUserContent", {}, token_v2)
    
    space_view_id = None
    for sv_id, sv_rec in data.get("recordMap", {}).get("space_view", {}).items():
        if sv_rec.get("value", {}).get("space_id") == space_id:
            space_view_id = sv_id
            break

    if not space_view_id:
        raise RuntimeError(f"Could not find space_view for space {space_id}")

    # 2. Add to sidebar
    ops = [{
        "pointer": {"table": "space_view", "id": space_view_id, "spaceId": space_id},
        "path": ["settings", "sidebar_workflow_ids"],
        "command": "listAfter",
        "args": {"id": workflow_id}
    }]
    send_ops(space_id, ops, token_v2, user_id, user_action="sidebarWorkflowsActions.addSidebarWorkflow")


# ── Publish ───────────────────────────────────────────────────────────────────

def publish_agent(workflow_id: str, space_id: str,
                  token_v2: str, user_id: str | None = None,
                  dry_run: bool = False,
                  archive_existing: bool = True) -> dict:
    """
    Publish a Notion AI Agent workflow.
    Returns {workflowArtifactId, version} on success.

    archive_existing: If True, clear out old chats (standard for instruction updates).
                      If False, keep existing chats (used during initial creation).
    """
    payload = {"workflowId": workflow_id, "spaceId": space_id}

    try:
        result = _post("publishCustomAgentVersion", payload, token_v2, user_id, dry_run)
    except RuntimeError as e:
        # ... (rest of error handling unchanged) ...
        err_str = str(e)
        if "incomplete_ancestor_path" in err_str:
            result = {
                "warning": "incomplete_ancestor_path",
                "detail": (
                    "publishCustomAgentVersion returned incomplete_ancestor_path. "
                    "This also occurs in the Notion UI — block edits were saved "
                    "successfully but the publish/snapshot step failed. The agent "
                    "instructions are updated; the published artifact may be stale."
                ),
            }
        else:
            raise

    if dry_run:
        return result

    if archive_existing:
        try:
            cleanup = archive_workflow_threads(workflow_id, space_id, token_v2, user_id)
            result["archivedThreadCount"] = cleanup["count"]
            result["archivedThreadIds"] = cleanup["threadIds"]
        except Exception as e:
            result["threadCleanupWarning"] = str(e)

    if "warning" in result:
        return result

    if "workflowArtifactId" not in result:
        detail = f"Missing workflowArtifactId: {result}"
        result.setdefault("warning", "unexpected_response")
        result.setdefault("detail", detail)

    return result
