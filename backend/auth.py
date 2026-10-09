"""Local authentication with encrypted password transport and shared browser sessions."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from psycopg.rows import dict_row


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://atoms:atoms_local_password@localhost:25432/atoms_demo")
PRIVATE_KEY_PATH = Path(os.getenv("AUTH_PRIVATE_KEY_PATH", "/app/.auth_private.pem"))
ACCESS_SECONDS = 7 * 24 * 60 * 60
REFRESH_SECONDS = 7 * 24 * 60 * 60
COOKIE_NAME = "atoms_refresh"
password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
router = APIRouter(prefix="/api/auth", tags=["auth"])
_private_key = None


def connection():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_auth_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            access_hash TEXT NOT NULL UNIQUE,
            refresh_hash TEXT NOT NULL UNIQUE,
            access_expires_at TIMESTAMPTZ NOT NULL,
            refresh_expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS auth_sessions_user_idx ON auth_sessions(user_id)")
    # Tabs share one HttpOnly refresh cookie, but each keeps its own access token.
    # Refreshing a tab must not revoke tokens held by other tabs in that session.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_access_tokens (
            access_hash TEXT PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES auth_sessions(id) ON DELETE CASCADE,
            expires_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS auth_access_tokens_session_idx ON auth_access_tokens(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS auth_access_tokens_expiry_idx ON auth_access_tokens(expires_at)")
    conn.execute("""INSERT INTO auth_access_tokens(access_hash,session_id,expires_at)
        SELECT access_hash,id,access_expires_at FROM auth_sessions
        WHERE access_expires_at>NOW() AND refresh_expires_at>NOW()
        ON CONFLICT(access_hash) DO NOTHING""")
    get_private_key()


def get_private_key():
    global _private_key
    if _private_key is None:
        if not PRIVATE_KEY_PATH.exists():
            PRIVATE_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            try:
                fd = os.open(PRIVATE_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(pem)
            except FileExistsError:
                pass
        _private_key = serialization.load_pem_private_key(PRIVATE_KEY_PATH.read_bytes(), password=None)
    return _private_key


def token_hash(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def issue_session(conn, user_id: uuid.UUID, response: Response, session_id: uuid.UUID | None = None, refresh_token: str | None = None):
    access = secrets.token_urlsafe(32)
    if session_id and not refresh_token:
        raise ValueError('An existing session requires its refresh token')
    # Keep the shared cookie stable for the lifetime of this browser session.
    # It remains random, HttpOnly and server-revocable, with the existing expiry.
    refresh = refresh_token or secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    access_expiry = now + timedelta(seconds=ACCESS_SECONDS)
    refresh_expiry = now + timedelta(seconds=REFRESH_SECONDS)
    if session_id:
        conn.execute("UPDATE auth_sessions SET access_hash=%s,refresh_hash=%s,access_expires_at=%s,refresh_expires_at=%s WHERE id=%s", (token_hash(access), token_hash(refresh), access_expiry, refresh_expiry, session_id))
    else:
        session_id = uuid.uuid4()
        conn.execute("INSERT INTO auth_sessions(id,user_id,access_hash,refresh_hash,access_expires_at,refresh_expires_at) VALUES(%s,%s,%s,%s,%s,%s)", (session_id, user_id, token_hash(access), token_hash(refresh), access_expiry, refresh_expiry))
    conn.execute('DELETE FROM auth_access_tokens WHERE expires_at<=NOW()')
    conn.execute('INSERT INTO auth_access_tokens(access_hash,session_id,expires_at) VALUES(%s,%s,%s)', (token_hash(access), session_id, access_expiry))
    response.set_cookie(COOKIE_NAME, refresh, max_age=REFRESH_SECONDS, httponly=True,
                        secure=os.getenv("AUTH_SECURE_COOKIE", "false").lower() == "true",
                        samesite="lax", path="/api/auth")
    return {"access_token": access, "expires_in": ACCESS_SECONDS}


def decrypt_password(encrypted: str):
    try:
        ciphertext = base64.b64decode(encrypted, validate=True)
        password = get_private_key().decrypt(ciphertext, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)).decode("utf-8")
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        raise HTTPException(400, "密码加密数据无效") from exc
    if not 8 <= len(password.encode("utf-8")) <= 256:
        raise HTTPException(422, "密码长度需为 8–256 字节")
    return password


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    encrypted_password: str = Field(min_length=1, max_length=1024)
    invite_code: str | None = Field(default=None, max_length=100)


def user_json(row):
    return {"id": str(row["id"]), "email": row["email"]}


@router.get("/public-key")
def public_key():
    pem = get_private_key().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return {"algorithm": "RSA-OAEP-256", "public_key": pem}


@router.get("/session")
def session_status(atoms_refresh: str | None = Cookie(default=None)):
    if not atoms_refresh:
        return {"authenticated": False}
    with connection() as conn:
        row = conn.execute("SELECT 1 FROM auth_sessions WHERE refresh_hash=%s AND refresh_expires_at>NOW()", (token_hash(atoms_refresh),)).fetchone()
    return {"authenticated": bool(row)}


@router.post("/register", status_code=201)
def register(data: Credentials, response: Response):
    password = decrypt_password(data.encrypted_password)
    email = data.email.strip().lower()
    with connection() as conn:
        try:
            row = conn.execute("INSERT INTO users(id,email,password_hash) VALUES(%s,%s,%s) RETURNING id,email", (uuid.uuid4(), email, password_hasher.hash(password))).fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(409, "该邮箱已注册") from exc
        from referrals import attribute
        attribute(conn,row["id"],data.invite_code)
        tokens = issue_session(conn, row["id"], response)
    return {**tokens, "user": user_json(row)}


@router.post("/login")
def login(data: Credentials, response: Response):
    password = decrypt_password(data.encrypted_password)
    email = data.email.strip().lower()
    with connection() as conn:
        row = conn.execute("SELECT id,email,password_hash FROM users WHERE email=%s", (email,)).fetchone()
        if not row:
            raise HTTPException(401, "邮箱或密码错误")
        try:
            password_hasher.verify(row["password_hash"], password)
        except (VerifyMismatchError, InvalidHashError) as exc:
            raise HTTPException(401, "邮箱或密码错误") from exc
        if password_hasher.check_needs_rehash(row["password_hash"]):
            conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (password_hasher.hash(password), row["id"]))
        tokens = issue_session(conn, row["id"], response)
    return {**tokens, "user": user_json(row)}


@router.post("/refresh")
def refresh(response: Response, atoms_refresh: str | None = Cookie(default=None)):
    if not atoms_refresh:
        raise HTTPException(401, "会话已过期")
    with connection() as conn:
        row = conn.execute("SELECT s.id,s.user_id,u.email FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.refresh_hash=%s AND s.refresh_expires_at>NOW() FOR UPDATE OF s", (token_hash(atoms_refresh),)).fetchone()
        if not row:
            response.delete_cookie(COOKIE_NAME, path="/api/auth")
            raise HTTPException(401, "会话已过期")
        tokens = issue_session(conn, row["user_id"], response, row["id"], atoms_refresh)
    return {**tokens, "user": user_json({"id": row["user_id"], "email": row["email"]})}


@router.post("/logout", status_code=204)
def logout(response: Response, atoms_refresh: str | None = Cookie(default=None)):
    if atoms_refresh:
        with connection() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE refresh_hash=%s", (token_hash(atoms_refresh),))
    response.delete_cookie(COOKIE_NAME, path="/api/auth")


def current_user(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "请先登录")
    access = authorization[7:].strip()
    with connection() as conn:
        row = conn.execute("""SELECT u.id,u.email FROM auth_access_tokens t
            JOIN auth_sessions s ON s.id=t.session_id JOIN users u ON u.id=s.user_id
            WHERE t.access_hash=%s AND t.expires_at>NOW() AND s.refresh_expires_at>NOW()""", (token_hash(access),)).fetchone()
    if not row:
        raise HTTPException(401, "会话已过期")
    return row
