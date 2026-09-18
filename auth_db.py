"""MongoDB-backed account and authentication storage for TradeVerse."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash

import mongo_db

USERS_COLLECTION = os.getenv("MONGODB_USERS_COLLECTION", "users").strip() or "users"
ROLES = ("USER", "ADMIN")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")


def _collection():
    return mongo_db.get_collection(USERS_COLLECTION)


def _utc_now():
    return datetime.now(timezone.utc)


def normalize_username(username: str) -> str:
    return " ".join((username or "").strip().split())


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def normalize_phone(phone: str) -> str:
    return (phone or "").strip()


def validate_username(username: str) -> Optional[str]:
    username = normalize_username(username)
    if not USERNAME_RE.fullmatch(username):
        return "Username must be 3–30 characters and use only letters, numbers, dots, underscores, or hyphens."
    return None


def validate_email(email: str) -> Optional[str]:
    email = normalize_email(email)
    if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
        return "Please enter a valid email address."
    return None


def validate_phone(phone: str) -> Optional[str]:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 10 or len(digits) > 15:
        return "Please enter a valid phone number."
    return None


def init_indexes() -> None:
    collection = _collection()
    collection.create_index([("username_normalized", ASCENDING)], unique=True, name="username_unique")
    collection.create_index([("email", ASCENDING)], unique=True, name="email_unique")
    collection.create_index([("role", ASCENDING)], name="role_index")
    collection.create_index([("created_at", ASCENDING)], name="created_at_index")


def public_user(user: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not user:
        return None
    return {
        "id": user.get("id"),
        "username": user.get("username"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "role": user.get("role", "USER"),
        "created_at": user.get("created_at"),
    }


def get_user_by_username(username: str) -> Optional[dict[str, Any]]:
    return _collection().find_one(
        {"username_normalized": normalize_username(username).lower()},
        {"_id": 0},
    )


def get_user_by_id(user_id: str) -> Optional[dict[str, Any]]:
    return _collection().find_one({"id": user_id}, {"_id": 0})


def create_user(username: str, email: str, phone: str, password: str, role: str = "USER") -> dict[str, Any]:
    username = normalize_username(username)
    email = normalize_email(email)
    phone = normalize_phone(phone)
    role = (role or "USER").upper()

    validation_error = validate_username(username) or validate_email(email) or validate_phone(phone)
    if validation_error:
        raise ValueError(validation_error)
    if role not in ROLES:
        raise ValueError("Invalid account role.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters long.")

    user = {
        "id": uuid.uuid4().hex,
        "username": username,
        "username_normalized": username.lower(),
        "email": email,
        "phone": phone,
        "password_hash": generate_password_hash(password),
        "role": role,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }

    try:
        _collection().insert_one(user)
    except DuplicateKeyError as exc:
        raise ValueError("Username or email already exists.") from exc

    user.pop("_id", None)
    return user


def verify_credentials(username: str, password: str) -> Optional[dict[str, Any]]:
    from werkzeug.security import check_password_hash

    user = get_user_by_username(username)
    if user and check_password_hash(user.get("password_hash", ""), password or ""):
        return user
    return None


def ensure_admin_user() -> Optional[dict[str, Any]]:
    """Create the bootstrap ADMIN from environment variables if configured.

    Existing accounts are not overwritten. If the configured admin identity
    already exists, its role is ensured to be ADMIN.
    """
    username = normalize_username(os.getenv("ADMIN_USERNAME", "admin"))
    email = normalize_email(os.getenv("ADMIN_EMAIL", "admin@example.com"))
    phone = normalize_phone(os.getenv("ADMIN_PHONE", "0000000000"))
    password = os.getenv("ADMIN_PASSWORD", "")

    existing = get_user_by_username(username)
    if existing:
        if existing.get("role") != "ADMIN":
            _collection().update_one(
                {"id": existing["id"]},
                {"$set": {"role": "ADMIN", "updated_at": _utc_now()}},
            )
            existing["role"] = "ADMIN"
        return existing

    if not password:
        print("[startup] ADMIN_PASSWORD is not set; bootstrap admin was not created.")
        return None

    user = create_user(username, email, phone, password, role="ADMIN")
    return user


def count_users() -> int:
    return _collection().count_documents({})


def list_users(limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    cursor = _collection().find(
        {},
        {"_id": 0, "id": 1, "username": 1, "email": 1, "phone": 1, "role": 1, "created_at": 1},
    ).sort("created_at", ASCENDING).limit(limit)
    return list(cursor)
