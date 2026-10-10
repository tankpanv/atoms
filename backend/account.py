"""Account preferences and real balances backed by local simulated payments."""
import base64
import hashlib
import os
import json
from pathlib import Path
import uuid
from decimal import Decimal
from fastapi.responses import Response

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from auth import connection, current_user
from model_catalog import DEFAULT_MODEL, catalog_ids
from billing import RATE, create_order, pay_order, grant_due, ledger
from model_catalog import catalog

router = APIRouter(prefix="/api/account", tags=["account"])
DEFAULTS = {"language": "zh", "theme": "light", "default_model": DEFAULT_MODEL,
            "visibility": "public", "show_credits": False, "sound": "first",
            "email_notifications": True, "remove_badge": False, "storage_metered": False}


def init_account_db(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS account_profiles (
        user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        display_name TEXT NOT NULL, avatar TEXT NOT NULL DEFAULT '',
        workspace_name TEXT NOT NULL, workspace_description TEXT NOT NULL DEFAULT '',
        workspace_avatar TEXT NOT NULL DEFAULT '', preferences JSONB NOT NULL DEFAULT '{}',
        credits NUMERIC(16,4) NOT NULL DEFAULT 0, plan TEXT NOT NULL DEFAULT 'Free')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_credit_codes (
        code_hash TEXT PRIMARY KEY, credits NUMERIC(16,4) NOT NULL CHECK(credits>0),
        expires_at TIMESTAMPTZ, redeemed_by UUID REFERENCES users(id), redeemed_at TIMESTAMPTZ)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_transactions (
        id BIGSERIAL PRIMARY KEY, user_id UUID REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, amount NUMERIC(16,4) NOT NULL, description TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def ensure_profile(conn, user):
    name = user["email"].split("@")[0]
    conn.execute("""INSERT INTO account_profiles(user_id,display_name,workspace_name)
        VALUES(%s,%s,%s) ON CONFLICT(user_id) DO NOTHING""", (user["id"], name, name + "'s Atoms"))
    grant_due(conn, user['id'])
    return conn.execute("SELECT a.*,u.created_at FROM account_profiles a JOIN users u ON u.id=a.user_id WHERE a.user_id=%s", (user["id"],)).fetchone()


def serialize(row, user):
    return {**row, "user_id": str(row["user_id"]), "email": user["email"],
            "credits": float(row["credits"]), "preferences": {**DEFAULTS, **row["preferences"]},
            "available_credits": float(max(Decimal(0), row['credits'] - row['reserved_credits'])),
            "credits_per_usd": float(RATE), "payment_mode": "simulation",
            "created_at": row.get("created_at"),
            "billing_enabled": True, "checkout_enabled": True}


@router.get("")
def get_account(user=Depends(current_user)):
    with connection() as conn:
        return serialize(ensure_profile(conn, user), user)


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    avatar: str | None = Field(default=None, max_length=3_000_000)
    workspace_name: str | None = Field(default=None, min_length=1, max_length=100)
    workspace_description: str | None = Field(default=None, max_length=500)
    workspace_avatar: str | None = Field(default=None, max_length=3_000_000)
    preferences: dict | None = None


@router.patch("")
def update_account(data: ProfileUpdate, user=Depends(current_user)):
    values = data.model_dump(exclude_none=True)
    for key in ("display_name", "workspace_name"):
        if key in values:
            values[key] = values[key].strip()
            if not values[key]:
                raise HTTPException(422, "名称不能为空")
    for key in ("avatar", "workspace_avatar"):
        if values.get(key):
            value = values[key]
            try:
                header, payload = value.split(",", 1)
                raw = base64.b64decode(payload, validate=True)
                valid = ((header == "data:image/png;base64" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or
                         (header == "data:image/jpeg;base64" and raw.startswith(b"\xff\xd8\xff")) or
                         (header == "data:image/webp;base64" and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"))
                if not valid or len(raw) > 2_000_000:
                    raise ValueError()
            except (ValueError, TypeError):
                raise HTTPException(422, "请上传 2MB 以内的 PNG、JPEG 或 WebP 图片")
    if "preferences" in values:
        prefs = values["preferences"]
        enums = {"language": ["zh", "en"], "theme": ["light", "dark", "system"],
                 "visibility": ["public", "private"], "sound": ["first", "always", "off"]}
        for key, value in prefs.items():
            if key not in DEFAULTS or (key in enums and value not in enums[key]):
                raise HTTPException(422, "无效的偏好设置")
            if isinstance(DEFAULTS.get(key), bool) and not isinstance(value, bool):
                raise HTTPException(422, "偏好设置必须为布尔值")
        if "default_model" in prefs and prefs["default_model"] not in catalog_ids():
            raise HTTPException(422, "模型不在 model_list 中")
    with connection() as conn:
        ensure_profile(conn, user)
        # Lock before merging JSON to avoid losing simultaneous preference updates.
        row = conn.execute("SELECT * FROM account_profiles WHERE user_id=%s FOR UPDATE", (user["id"],)).fetchone()
        for key, value in values.items():
            if key == "preferences":
                value = Jsonb({**row["preferences"], **value})
            # Keys come exclusively from the Pydantic model.
            conn.execute(f"UPDATE account_profiles SET {key}=%s WHERE user_id=%s", (value, user["id"]))
        return serialize(ensure_profile(conn, user), user)


class RedeemInput(BaseModel):
    code: str = Field(min_length=1, max_length=200)


@router.post("/redeem")
def redeem(data: RedeemInput, user=Depends(current_user)):
    hashed = hashlib.sha256(data.code.strip().encode()).hexdigest()
    with connection() as conn:
        ensure_profile(conn, user)
        code = conn.execute("""UPDATE account_credit_codes SET redeemed_by=%s,redeemed_at=NOW()
            WHERE code_hash=%s AND redeemed_by IS NULL AND (expires_at IS NULL OR expires_at>NOW())
            RETURNING credits""", (user["id"], hashed)).fetchone()
        if not code:
            raise HTTPException(400, "兑换码无效、已过期或已被使用")
        ledger(conn, user['id'], code['credits'], 'redemption', '积分兑换')
        return serialize(ensure_profile(conn, user), user)


@router.get("/transactions")
def transactions(user=Depends(current_user), offset: int = Query(0, ge=0)):
    with connection() as conn:
        return conn.execute("SELECT id,kind,amount,description,balance_after,order_id,request_id,metadata,created_at FROM account_transactions WHERE user_id=%s ORDER BY id DESC LIMIT 200 OFFSET %s", (user["id"], offset)).fetchall()


@router.get("/storage")
def storage(user=Depends(current_user)):
    root = Path(os.getenv("WORKSPACE_ROOT", "/workspaces")).resolve()
    with connection() as conn:
        projects = conn.execute("SELECT id,title,workspace_path,updated_at FROM projects WHERE owner_id=%s", (user["id"],)).fetchall()
    result = []
    for project in projects:
        size = count = 0
        directory = (root / project["workspace_path"]).resolve() if project["workspace_path"] else root / "projects" / project["id"].hex
        if directory.is_relative_to(root) and directory.is_dir():
            for entry in directory.rglob("*"):
                try:
                    if entry.is_file() and not entry.is_symlink():
                        size += entry.stat().st_size
                        count += 1
                except OSError:
                    continue
        result.append({"id": str(project["id"]), "title": project["title"], "bytes": size, "files": count, "updated_at": project["updated_at"]})
    return {"projects": result, "total_bytes": sum(p["bytes"] for p in result)}


class BillingInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan: str = "Pro"
    credits: int = 350
    annual: bool = False
    amount_usd: Decimal = Decimal('10')
    idempotency_key: uuid.UUID


@router.post("/billing/{action}")
def billing(action: str, data: BillingInput, user=Depends(current_user)):
    if action not in ('checkout', 'topup'):
        raise HTTPException(404)
    with connection() as conn:
        ensure_profile(conn, user)
        return create_order(conn, user['id'], data.idempotency_key, 'topup' if action == 'topup' else 'subscription', data.amount_usd, data.plan, data.credits, data.annual)


class PaymentInput(BaseModel):
    outcome: str = Field(pattern='^(success|fail|cancel)$')


@router.post('/orders/{order_id}/pay')
def pay(order_id: uuid.UUID, data: PaymentInput, user=Depends(current_user)):
    with connection() as conn:
        ensure_profile(conn, user)
        order = pay_order(conn, user['id'], order_id, data.outcome)
        return {'order': order, 'account': serialize(ensure_profile(conn, user), user)}


@router.get('/orders')
def orders(user=Depends(current_user), offset: int = Query(0, ge=0)):
    with connection() as conn:
        conn.execute("UPDATE billing_orders SET status='expired' WHERE user_id=%s AND status='pending' AND expires_at<=NOW()", (user['id'],))
        return conn.execute('SELECT * FROM billing_orders WHERE user_id=%s ORDER BY created_at DESC LIMIT 200 OFFSET %s', (user['id'], offset)).fetchall()


@router.get('/orders/{order_id}/receipt')
def receipt(order_id: uuid.UUID, user=Depends(current_user)):
    with connection() as conn:
        order = conn.execute("SELECT * FROM billing_orders WHERE id=%s AND user_id=%s AND status='paid'", (order_id, user['id'])).fetchone()
    if not order:
        raise HTTPException(404, '已支付订单不存在')
    content = json.dumps({'receipt_type': 'simulation_payment_receipt', **order}, default=str, ensure_ascii=False, indent=2)
    return Response(content, media_type='application/json', headers={'Content-Disposition': f'attachment; filename="receipt-{order_id}.json"'})


@router.get('/usage')
def usage(user=Depends(current_user), offset: int = Query(0, ge=0)):
    with connection() as conn:
        ensure_profile(conn, user)
        requests = conn.execute("""SELECT b.id,b.project_id,p.title AS project_title,b.job_id,b.model,b.stage,b.status,
            b.prompt_tokens,b.completion_tokens,b.input_price,b.output_price,b.credits_per_usd,b.cost_usd,
            b.charged_credits,b.provider_cost_usd,b.provider_model,b.provider_source,b.parent_request_id,
            b.reserved,b.generation_id,b.error,b.created_at,
            COALESCE((b.usage->'prompt_tokens_details'->>'cached_tokens')::bigint,0) AS cached_tokens,
            COALESCE((b.usage->'prompt_tokens_details'->>'cache_write_tokens')::bigint,0) AS cache_write_tokens
            FROM billing_requests b LEFT JOIN projects p ON p.id=b.project_id WHERE b.user_id=%s
            ORDER BY b.created_at DESC LIMIT 200 OFFSET %s""", (user['id'], offset)).fetchall()
        totals = conn.execute("""SELECT COALESCE(SUM(charged_credits),0) AS credits,
            COALESCE(SUM(cost_usd),0) AS cost_usd,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
            COALESCE(SUM(completion_tokens),0) AS completion_tokens FROM billing_requests WHERE user_id=%s AND status='settled'""", (user['id'],)).fetchone()
    return {'requests': requests, 'totals': totals, 'credits_per_usd': RATE,
            'models': [{**m, 'input_credits_per_million': Decimal(str(m['input_price']))*RATE,
                        'output_credits_per_million': Decimal(str(m['output_price']))*RATE} for m in catalog()]}


@router.get("/connectors/authorize")
def authorize_connector(provider: str, user=Depends(current_user)):
    try:
        urls = json.loads(os.getenv("ACCOUNT_CONNECTOR_AUTH_URLS", "{}"))
    except (ValueError, TypeError):
        raise HTTPException(503, "连接器授权配置无效，请联系管理员")
    url = urls.get(provider, "") if isinstance(urls, dict) else ""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise HTTPException(503, f"{provider} 尚未配置授权服务，请联系管理员")
    return {"url": url}
