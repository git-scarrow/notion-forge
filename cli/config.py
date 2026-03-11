import os
import json
from dataclasses import dataclass
from typing import Any

# Global paths
RESOURCES_JSON = os.path.expanduser("~/.ai/resources.json")

# Default hardcoded values (as fallbacks if resources.json is missing)
DEFAULT_SPACE_ID = "f04bc8a1-18df-42d1-ba9f-961c491cdc1b"
DEFAULT_WORK_ITEMS_DB_ID = "daeb64d4-e5a8-4a7b-b0dc-7555cbc3def6"
DEFAULT_LAB_PROJECTS_DB_ID = "389645af-0e4f-479e-a910-79b169a99462"
DEFAULT_PROMPT_ENGINEERING_DB_ID = "47d13520-73fd-4d9f-bdc0-1f32fd3d6483"
DEFAULT_AUDIT_LOG_DB_ID = "f3a4d67a-5734-4ea4-908b-458f1c63f875"
DEFAULT_CHATSEARCH_PROJECT_ID = "f7cca113-ad21-4261-a170-4a88441a0e66"
DEFAULT_LIBRARIAN_WORKFLOW_ID = "882193ee-37d8-4367-95eb-49ddf86aed9d"
DEFAULT_LIBRARIAN_BOT_RUNTIME = "31ce7cc7-01d5-81e4-a7d6-0027b142ad0b"
DEFAULT_LIBRARIAN_BOT_DRAFT = "31ce7cc7-01d5-81de-9caf-00278a693357"

@dataclass(frozen=True)
class Config:
    notion_token: str
    space_id: str
    work_items_db_id: str
    lab_projects_db_id: str
    prompt_engineering_db_id: str
    audit_log_db_id: str
    chatsearch_project_id: str
    librarian_notion_internal_id: str
    librarian_bot_runtime: str
    librarian_bot_draft: str

    @classmethod
    def from_env(cls) -> "Config":
        # 1. Load data from ground truth resources.json if available
        res = {}
        if os.path.exists(RESOURCES_JSON):
            try:
                with open(RESOURCES_JSON, "r") as f:
                    res = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load {RESOURCES_JSON}: {e}")

        # Helper to get from nested resources structure
        def get_db_id(key, default):
            # Try to get notion_public_id from databases section
            return res.get("databases", {}).get(key, {}).get("notion_public_id", default)
        
        def get_agent_id(key, field, default):
            return res.get("agents", {}).get(key, {}).get(field, default)

        token = os.environ.get("NOTION_TOKEN")
        if not token:
            token_path = os.path.expanduser("~/.notion-token")
            if os.path.exists(token_path):
                with open(token_path, "r") as f:
                    token = f.read().strip()
        
        if not token:
            raise ValueError("NOTION_TOKEN environment variable required")

        return cls(
            notion_token=token,
            space_id=os.environ.get("NOTION_SPACE_ID", res.get("workspace", {}).get("space_id", DEFAULT_SPACE_ID)),
            work_items_db_id=os.environ.get("WORK_ITEMS_DB_ID", get_db_id("work_items", DEFAULT_WORK_ITEMS_DB_ID)),
            lab_projects_db_id=os.environ.get("LAB_PROJECTS_DB_ID", get_db_id("lab_projects", DEFAULT_LAB_PROJECTS_DB_ID)),
            prompt_engineering_db_id=os.environ.get("PROMPT_ENGINEERING_DB_ID", get_db_id("prompt_engineering", DEFAULT_PROMPT_ENGINEERING_DB_ID)),
            audit_log_db_id=os.environ.get("AUDIT_LOG_DB_ID", get_db_id("lab_audit_log", DEFAULT_AUDIT_LOG_DB_ID)),
            chatsearch_project_id=os.environ.get("CHATSEARCH_PROJECT_ID", DEFAULT_CHATSEARCH_PROJECT_ID),
            librarian_notion_internal_id=os.environ.get("LIBRARIAN_WORKFLOW_ID", get_agent_id("lab_librarian_knowledge_synthesis", "notion_internal_id", DEFAULT_LIBRARIAN_WORKFLOW_ID)),
            librarian_bot_runtime=os.environ.get("LIBRARIAN_BOT_RUNTIME", get_agent_id("lab_librarian_knowledge_synthesis", "notion_internal_id", DEFAULT_LIBRARIAN_BOT_RUNTIME)),
            librarian_bot_draft=os.environ.get("LIBRARIAN_BOT_DRAFT", DEFAULT_LIBRARIAN_BOT_DRAFT),
        )

# Global config instance
try:
    config = Config.from_env()
except Exception:
    config = None

def get_config() -> Config:
    if config is None:
        return Config.from_env()
    return config
