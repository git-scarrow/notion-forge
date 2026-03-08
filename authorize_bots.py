import sys
import os
import json

sys.path.insert(0, os.path.join(os.getcwd(), "cli"))
import notion_client
import cookie_extract

def authorize_bots():
    token, user_id = cookie_extract.get_auth()
    space_id = 'f04bc8a1-18df-42d1-ba9f-961c491cdc1b'
    db_id = 'daeb64d4-e5a8-4a7b-b0dc-7555cbc3def6' # Work Items
    
    # Lab Librarian Bots
    bots = [
        '31ce7cc7-01d5-81e4-a7d6-0027b142ad0b', # Runtime
        '31ce7cc7-01d5-81de-9caf-00278a693357'  # Draft
    ]
    
    ops = []
    for bot_id in bots:
        # Use setPermission command which is the standard for block access
        ops.append({
            "pointer": {"table": "block", "id": db_id, "spaceId": space_id},
            "path": [],
            "command": "setPermission",
            "args": {
                "role": "editor",
                "type": "bot_permission",
                "bot_id": bot_id
            }
        })

    print(f"Sending setPermission ops for {len(bots)} bots...")
    notion_client.send_ops(space_id, ops, token, user_id, user_action="agentPersistenceActions.addPage")
    print("✓ Permissions sent. Publishing agent to sync...")
    
    # Final publish to ensure the backend refreshes the bot's worldview
    notion_client.publish_agent('882193ee-37d8-4367-95eb-49ddf86aed9d', space_id, token, user_id)
    print("✓ Lab Librarian re-published.")

if __name__ == "__main__":
    authorize_bots()
