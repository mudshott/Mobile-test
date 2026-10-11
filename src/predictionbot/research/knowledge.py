"""Research knowledge base: findings → HYPOTHESES (never automatic trading rules).

Seeds are inserted once; results and statuses recorded later are never
overwritten by a restart. Failed experiments are kept (status REJECTED/WEAK).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from predictionbot.persistence.db import Database

STATUSES = ("UNTESTED", "SUPPORTED", "WEAK", "REJECTED")
SEED = Path(__file__).resolve().parent / "knowledge.json"


def seed_knowledge(db: Database) -> int:
    items = json.loads(SEED.read_text(encoding="utf-8"))
    added = 0
    for it in items:
        if db.one("SELECT id FROM research_hypotheses WHERE id=?", (it["id"],)):
            continue
        db.insert("research_hypotheses", {**it, "updated_ts": time.time()})
        added += 1
    return added


def record_result(db: Database, hypothesis_id: str, status: str, result: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    db.execute(
        "UPDATE research_hypotheses SET status=?, result=?, updated_ts=? WHERE id=?",
        (status, result, time.time(), hypothesis_id),
    )


def list_hypotheses(db: Database) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM research_hypotheses ORDER BY id")
