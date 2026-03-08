#!/usr/bin/env python3
"""
create_agent.py — Create a new Notion AI Agent from scratch.

Usage:
  python cli/create_agent.py "My New Agent"
  python cli/create_agent.py "My New Agent" --icon "https://..."
  python cli/create_agent.py "My New Agent" --space-id <uuid>
"""

import argparse
import sys
import os
import yaml

# Allow running from project root or cli/ directory
sys.path.insert(0, os.path.dirname(__file__))

import cookie_extract
import notion_client

AGENTS_YAML = os.path.join(os.path.dirname(__file__), "agents.yaml")


def get_auth() -> tuple[str, str]:
    """Return (token_v2, user_id). Exits on failure."""
    try:
        token = cookie_extract.get_token_v2()
        user_id = cookie_extract.get_user_id()
    except (FileNotFoundError, ValueError) as e:
        print(f"Auth error: {e}", file=sys.stderr)
        sys.exit(1)

    if not user_id:
        print("Auth error: Could not find user_id in cookies.", file=sys.stderr)
        sys.exit(1)
        
    return token, user_id


def register_agent(name: str, workflow_id: str, space_id: str, block_id: str) -> None:
    """Add or update an agent entry in agents.yaml."""
    registry = {}
    if os.path.exists(AGENTS_YAML):
        with open(AGENTS_YAML) as f:
            registry = yaml.safe_load(f) or {}

    registry[name] = {
        "workflow_id": workflow_id,
        "space_id": space_id,
        "block_id": block_id
    }

    with open(AGENTS_YAML, "w") as f:
        yaml.safe_dump(registry, f, sort_keys=True)
    print(f"✓ Registered '{name}' in agents.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a new Notion AI Agent.")
    parser.add_argument("name", help="Name of the new agent")
    parser.add_argument("--icon", help="URL to an icon image")
    parser.add_argument("--space-id", help="Target Notion space ID (UUID)")
    parser.add_argument("--no-sidebar", action="store_true", help="Don't add to sidebar")

    args = parser.parse_args()
    token, user_id = get_auth()

    # ... (space discovery logic same as before) ...
    space_id = args.space_id
    if not space_id:
        spaces = notion_client.get_user_spaces(token)
        if len(spaces) == 1:
            space_id = spaces[0]["id"]
            print(f"Using only available space: {spaces[0]['name']} ({space_id})")
        elif len(spaces) > 1:
            print("Multiple spaces found. Please specify one with --space-id:")
            for s in spaces:
                print(f"  {s['id']}  {s['name']} ({s['domain']})")
            sys.exit(1)
        else:
            print("Error: No spaces found for this user.", file=sys.stderr)
            sys.exit(1)

    print(f"Creating agent '{args.name}'...")
    result = notion_client.create_agent(space_id, args.name, args.icon, token, user_id)
    wf_id = result["workflow_id"]
    block_id = result["block_id"]

    print(f"✓ Created workflow: {wf_id}")
    print(f"✓ Created instruction block: {block_id}")

    if not args.no_sidebar:
        print("Adding to sidebar...")
        notion_client.add_agent_to_sidebar(space_id, wf_id, token, user_id)
        print("✓ Added to sidebar")

    print("Publishing...")
    # archive_existing=False ensures we don't try to clear non-existent threads
    notion_client.publish_agent(wf_id, space_id, token, user_id, archive_existing=False)
    print("✓ Published")

    register_agent(args.name, wf_id, space_id, block_id)
    print(f"\nSuccess! Agent '{args.name}' is ready.")
    print(f"Manage it with: python cli/update_agent.py \"{args.name}\" --dump")


if __name__ == "__main__":
    main()
