"""Shared MongoDB Atlas connection for TradeVerse.

The connection is created lazily and cached so warm Vercel invocations reuse the
same PyMongo client. MongoDB Atlas is the durable application datastore for both
accounts and portfolios; this avoids relying on Vercel's ephemeral filesystem.
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

_client: Optional[MongoClient] = None
_db = None


def get_mongodb_uri() -> str:
    return os.getenv("MONGODB_URI", "").strip()


def get_database_name() -> str:
    return os.getenv("MONGODB_DB_NAME", "tradeverse").strip() or "tradeverse"


def get_client() -> MongoClient:
    global _client

    uri = get_mongodb_uri()
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not configured. Add the MongoDB Atlas connection "
            "string to your local .env file and to Vercel Environment Variables."
        )

    if _client is None:
        _client = MongoClient(
            uri,
            serverSelectionTimeoutMS=int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "8000")),
            connectTimeoutMS=int(os.getenv("MONGODB_CONNECT_TIMEOUT_MS", "8000")),
            socketTimeoutMS=int(os.getenv("MONGODB_SOCKET_TIMEOUT_MS", "10000")),
            maxPoolSize=int(os.getenv("MONGODB_MAX_POOL_SIZE", "10")),
            retryWrites=True,
        )

    return _client


def get_database():
    global _db
    if _db is None:
        _db = get_client()[get_database_name()]
    return _db


def get_collection(name: str):
    name = (name or "").strip()
    if not name:
        raise ValueError("MongoDB collection name cannot be empty.")
    return get_database()[name]


def ping() -> None:
    """Force a lightweight connectivity check when explicitly requested."""
    get_client().admin.command("ping")
