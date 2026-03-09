import sys
import os
import json

sys.path.insert(0, os.path.join(os.getcwd(), "cli"))
import notion_client
import cookie_extract
import config

# Use config instance
CFG = config.get_config()

def authorize_bots():
    # Use config token if available, otherwise cookie_extract
    token = CFG.notion_token if CFG and CFG.notion_token else cookie_extract.get_token_v2()
    user_id = cookie_extract.get_user_id()
    
    space_id = CFG.space_id
    db_id = CFG.work_items_db_id
    
    # Lab Librarian Bots
    bots = [
        CFG.librarian_bot_runtime,
        CFG.librarian_bot_draft
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
    notion_client.publish_agent(CFG.librarian_notion_internal_id, space_id, token, user_id)
    print("✓ Lab Librarian re-published.")

if __name__ == "__main__":
    authorize_bots()
