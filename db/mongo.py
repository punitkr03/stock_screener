"""
db/mongo.py
-----------
Lightweight MongoDB helper for the NSE scanner pipeline.

Usage
-----
from db.mongo import write_collection

# Write a list of dicts (one document per entry):
write_collection("buy_confirmed_data", entries)

# Write a single envelope dict as one document:
write_collection("indices_data", output_dict)
"""

from __future__ import annotations

from typing import Any

from pymongo import MongoClient

import config


def _get_db():
    """Return a pymongo database handle using config settings."""
    client = MongoClient(config.MONGODB_URI)
    return client[config.MONGODB_DB]


def write_collection(collection_name: str, data: list[dict] | dict) -> int:
    """
    Clear *all* existing documents in `collection_name`, then insert `data`.

    Parameters
    ----------
    collection_name : str
        Target MongoDB collection (e.g. "buy_confirmed_data").
    data : list[dict] | dict
        - If a **list**, each element is inserted as a separate document.
        - If a **dict**, it is wrapped in a list and inserted as one document.

    Returns
    -------
    int
        Number of documents inserted.
    """
    db = _get_db()
    col = db[collection_name]

    # ── 1. Clear previous documents ──────────────────────────────────────────
    deleted = col.delete_many({})
    if deleted.deleted_count:
        print(f"[MongoDB] '{collection_name}': cleared {deleted.deleted_count} old document(s)")

    # ── 2. Normalise to a list ────────────────────────────────────────────────
    documents: list[dict] = data if isinstance(data, list) else [data]

    if not documents:
        print(f"[MongoDB] '{collection_name}': no documents to insert — skipping")
        return 0

    # ── 3. Remove any stale _id fields (safety guard for re-used dicts) ───────
    for doc in documents:
        doc.pop("_id", None)

    # ── 4. Insert ─────────────────────────────────────────────────────────────
    result = col.insert_many(documents)
    count = len(result.inserted_ids)
    print(f"[MongoDB] '{collection_name}': inserted {count} document(s)")
    return count
