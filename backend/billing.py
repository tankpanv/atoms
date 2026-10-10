"""Persistent orders, append-only ledger and per-request model reservations.

Payment is deliberately a local simulator. All balances and AI usage are real.
Workers never have access to account balances or the upstream API credential.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from model_catalog import catalog, canonical_model_id

RATE = Decimal('5')
QUANTUM = Decimal('0.00000001')
TIERS = {'Free': [0], 'Pro': [100, 250, 350], 'Max': [500, 1000, 1500, 2500, 3750, 5000, 6000, 7500, 10000, 12500, 15000]}


def quantize(value):
    return Decimal(str(value)).quantize(QUANTUM, rounding=ROUND_HALF_UP)


def next_month(date):
    month = date.month % 12 + 1
    year = date.year + (date.month == 12)
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1]))


def init_billing_db(conn):
    conn.execute('ALTER TABLE account_profiles ALTER COLUMN credits TYPE NUMERIC(24,8)')
    conn.execute('ALTER TABLE account_transactions ALTER COLUMN amount TYPE NUMERIC(24,8)')
    for column, declaration in [('reserved_credits', 'NUMERIC(24,8) NOT NULL DEFAULT 0'),
                                ('plan_credits', 'INTEGER NOT NULL DEFAULT 0'),
                                ('plan_interval', "TEXT NOT NULL DEFAULT 'month'"),
                                ('paid_until', 'TIMESTAMPTZ'), ('next_grant_at', 'TIMESTAMPTZ'),
                                ('free_grant_day', 'DATE'), ('free_grant_month', "TEXT NOT NULL DEFAULT ''"),
                                ('free_granted', 'NUMERIC(24,8) NOT NULL DEFAULT 0')]:
        conn.execute(f'ALTER TABLE account_profiles ADD COLUMN IF NOT EXISTS {column} {declaration}')
    conn.execute("""CREATE TABLE IF NOT EXISTS billing_orders (
        id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        idempotency_key UUID NOT NULL, kind TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '',
        plan_credits INTEGER NOT NULL DEFAULT 0, annual BOOLEAN NOT NULL DEFAULT FALSE,
        amount_usd NUMERIC(24,8) NOT NULL, credits NUMERIC(24,8) NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        paid_at TIMESTAMPTZ, expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW()+INTERVAL '30 minutes',
        UNIQUE(user_id,idempotency_key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS billing_requests (
        id UUID PRIMARY KEY, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id UUID REFERENCES projects(id) ON DELETE SET NULL,
        job_id UUID REFERENCES agent_jobs(id) ON DELETE SET NULL,
        model TEXT NOT NULL, stage TEXT NOT NULL, input_price NUMERIC(24,8) NOT NULL,
        output_price NUMERIC(24,8) NOT NULL, credits_per_usd NUMERIC(24,8) NOT NULL,
        reserved NUMERIC(24,8) NOT NULL, status TEXT NOT NULL DEFAULT 'reserved',
        generation_id TEXT UNIQUE, prompt_tokens BIGINT, completion_tokens BIGINT,
        cost_usd NUMERIC(24,12), charged_credits NUMERIC(24,8), provider_cost_usd NUMERIC(24,12),
        usage JSONB NOT NULL DEFAULT '{}', response JSONB, error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS payload_hash TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS provider_model TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS attempt_finished BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS retry_safe BOOLEAN NOT NULL DEFAULT FALSE")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS provider_source TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS provider_base_url TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS routed_request_id UUID")
    conn.execute("ALTER TABLE billing_requests ADD COLUMN IF NOT EXISTS parent_request_id UUID")
    # Historical explicit SSE failures are terminal, unlike socket interruptions.
    conn.execute("""UPDATE billing_requests SET attempt_finished=TRUE,retry_safe=TRUE
        WHERE NOT attempt_finished AND error LIKE '模型流式返回错误：%%'
        AND (error LIKE '%%''code'': 502%%' OR error LIKE '%%''code'': 503%%'
             OR error LIKE '%%''code'': 500%%' OR error LIKE '%%''code'': 429%%')""")
    for column, declaration in [('balance_after', 'NUMERIC(24,8)'), ('order_id', 'UUID REFERENCES billing_orders(id) ON DELETE SET NULL'),
                                ('request_id', 'UUID REFERENCES billing_requests(id) ON DELETE SET NULL'),
                                ('metadata', "JSONB NOT NULL DEFAULT '{}'::jsonb")]:
        conn.execute(f'ALTER TABLE account_transactions ADD COLUMN IF NOT EXISTS {column} {declaration}')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS billing_request_transaction ON account_transactions(request_id) WHERE request_id IS NOT NULL')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS billing_order_transaction ON account_transactions(order_id) WHERE order_id IS NOT NULL')
    conn.execute('CREATE INDEX IF NOT EXISTS billing_requests_user_created ON billing_requests(user_id,created_at DESC)')


def ledger(conn, user_id, amount, kind, description, *, order_id=None, request_id=None, metadata=None):
    amount = quantize(amount)
    row = conn.execute('UPDATE account_profiles SET credits=credits+%s WHERE user_id=%s RETURNING credits', (amount, user_id)).fetchone()
    conn.execute("""INSERT INTO account_transactions(user_id,kind,amount,description,balance_after,order_id,request_id,metadata)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""", (user_id, kind, amount, description, row['credits'], order_id, request_id, Jsonb(metadata or {})))
    return row['credits']


def grant_due(conn, user_id, now=None):
    now = now or datetime.now(timezone.utc)
    row = conn.execute('SELECT * FROM account_profiles WHERE user_id=%s FOR UPDATE', (user_id,)).fetchone()
    if not row:
        return
    due = row['next_grant_at']
    while due and row['paid_until'] and due < row['paid_until'] and due <= now:
        ledger(conn, user_id, row['plan_credits'], 'subscription_grant', '年付套餐每月积分到账', metadata={'period': due.isoformat()})
        due = next_month(due)
        conn.execute('UPDATE account_profiles SET next_grant_at=%s WHERE user_id=%s', (due, user_id))
    if row['paid_until'] and row['paid_until'] <= now:
        conn.execute("UPDATE account_profiles SET plan='Free',plan_credits=0,paid_until=NULL,next_grant_at=NULL WHERE user_id=%s", (user_id,))
    month = now.strftime('%Y-%m')
    granted = row['free_granted'] if row['free_grant_month'] == month else Decimal(0)
    if row['free_grant_day'] != now.date() or row['free_grant_month'] != month:
        amount = min(Decimal(15), Decimal(25) - granted)
        if amount > 0:
            ledger(conn, user_id, amount, 'daily_grant', '每日免费积分', metadata={'day': now.date().isoformat()})
            granted += amount
        conn.execute('UPDATE account_profiles SET free_grant_day=%s,free_grant_month=%s,free_granted=%s WHERE user_id=%s', (now.date(), month, granted, user_id))


def model_price(model):
    item = next((m for m in catalog() if m['id'] == canonical_model_id(model)), None)
    if not item:
        raise HTTPException(422, '模型未配置计费价格，不能发起调用')
    if item['input_price'] < 0 or item['output_price'] < 0:
        raise HTTPException(422, '模型价格配置无效')
    return item


def reserve_request(conn, request_id, project_id, job_id, model, stage, max_tokens, payload_hash=''):
    # Lock the owner before request creation: parallel calls cannot overspend.
    project = conn.execute('SELECT owner_id,status FROM projects WHERE id=%s FOR SHARE', (project_id,)).fetchone()
    if not project or not project['owner_id']:
        raise HTTPException(404, '项目账户不存在')
    if project['status'] == 'deleting':
        raise HTTPException(409, '项目正在删除，不能再发起模型调用')
    user_id = project['owner_id']
    row = conn.execute('SELECT * FROM account_profiles WHERE user_id=%s FOR UPDATE', (user_id,)).fetchone()
    if not row:
        raise HTTPException(402, '请先打开账户页面领取积分或充值')
    existing = conn.execute('SELECT * FROM billing_requests WHERE id=%s', (request_id,)).fetchone()
    if existing:
        if existing['user_id'] != user_id or existing['job_id'] != job_id or existing['model'] != model or existing['stage'] != stage or existing['payload_hash'] != payload_hash:
            raise HTTPException(409, '调用标识已被其他请求使用')
        return existing, False
    job = conn.execute("SELECT 1 FROM agent_jobs WHERE id=%s AND project_id=%s AND status='running'", (job_id, project_id)).fetchone()
    if not job:
        raise HTTPException(409, '调用不属于当前运行任务')
    price = model_price(model)
    if max_tokens < 1 or max_tokens > price['context']:
        raise HTTPException(422, '输出 token 上限无效')
    # Full model context is a conservative upper bound, not a guessed token bill.
    reserve = quantize((Decimal(str(price['input_price'])) * price['context'] + Decimal(str(price['output_price'])) * max_tokens) / 1_000_000 * RATE)
    grant_due(conn, user_id)
    balance = conn.execute('SELECT credits,reserved_credits FROM account_profiles WHERE user_id=%s', (user_id,)).fetchone()
    if balance['credits'] - balance['reserved_credits'] < reserve:
        raise HTTPException(402, f"积分不足：可用 {balance['credits'] - balance['reserved_credits']:.8f}，本次最多冻结 {reserve:.8f}。请充值后继续。")
    conn.execute('UPDATE account_profiles SET reserved_credits=reserved_credits+%s WHERE user_id=%s', (reserve, user_id))
    result = conn.execute("""INSERT INTO billing_requests(id,user_id,project_id,job_id,model,stage,input_price,output_price,credits_per_usd,reserved,payload_hash)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (request_id, user_id, project_id, job_id, model, stage, Decimal(str(price['input_price'])), Decimal(str(price['output_price'])), RATE, reserve, payload_hash)).fetchone()
    return result, True


def settle_request(conn, request_id, usage, response=None):
    # Use the same account -> request lock order as reservation/rejection.
    initial = conn.execute('SELECT user_id FROM billing_requests WHERE id=%s', (request_id,)).fetchone()
    conn.execute('SELECT user_id FROM account_profiles WHERE user_id=%s FOR UPDATE', (initial['user_id'],))
    row = conn.execute('SELECT * FROM billing_requests WHERE id=%s FOR UPDATE', (request_id,)).fetchone()
    if row['status'] in ('settled', 'rejected'):
        return row
    prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if isinstance(prompt, bool) or isinstance(completion, bool) or not isinstance(prompt, int) or not isinstance(completion, int) or min(prompt, completion) < 0:
        raise ValueError('模型未返回有效真实 token 用量，等待对账，不能估算扣费')
    cost = (row['input_price'] * prompt + row['output_price'] * completion) / 1_000_000
    charge = quantize(cost * row['credits_per_usd'])
    conn.execute('UPDATE account_profiles SET reserved_credits=reserved_credits-%s WHERE user_id=%s', (row['reserved'], row['user_id']))
    ledger(conn, row['user_id'], -charge, 'model_usage', f"{row['model']} · {row['stage']}", request_id=request_id,
           metadata={'model': row['model'], 'prompt_tokens': prompt, 'completion_tokens': completion, 'input_price': str(row['input_price']), 'output_price': str(row['output_price']), 'cost_usd': str(cost), 'credits_per_usd': str(row['credits_per_usd'])})
    return conn.execute("""UPDATE billing_requests SET status='settled',prompt_tokens=%s,completion_tokens=%s,cost_usd=%s,
        charged_credits=%s,provider_cost_usd=%s,usage=%s,
        response=CASE WHEN project_id IS NULL THEN NULL ELSE COALESCE(%s,response) END,
        updated_at=NOW() WHERE id=%s RETURNING *""",
        (prompt, completion, cost, charge, usage.get('cost'), Jsonb(usage), Jsonb(response) if response and row['project_id'] is not None else None, request_id)).fetchone()


def reject_request(conn, request_id, error):
    initial = conn.execute('SELECT user_id FROM billing_requests WHERE id=%s', (request_id,)).fetchone()
    conn.execute('SELECT user_id FROM account_profiles WHERE user_id=%s FOR UPDATE', (initial['user_id'],))
    row = conn.execute('SELECT * FROM billing_requests WHERE id=%s FOR UPDATE', (request_id,)).fetchone()
    if row['status'] in ('settled', 'rejected'):
        return
    conn.execute('UPDATE account_profiles SET reserved_credits=reserved_credits-%s WHERE user_id=%s', (row['reserved'], row['user_id']))
    conn.execute("UPDATE billing_requests SET status='rejected',error=%s,updated_at=NOW() WHERE id=%s", (error[:1200], request_id))


def create_order(conn, user_id, key, kind, amount=None, plan='Pro', credits=350, annual=False):
    conn.execute('SELECT user_id FROM account_profiles WHERE user_id=%s FOR UPDATE', (user_id,))
    if kind == 'topup':
        amount = Decimal(str(amount))
        if not amount.is_finite() or amount < 1 or amount > 10000 or amount != amount.quantize(Decimal('.01')):
            raise HTTPException(422, '充值金额需为 $1–$10,000，最多两位小数')
        grant = amount * RATE
        plan, credits, annual = '', 0, False
    elif kind == 'subscription':
        if plan not in TIERS or credits not in TIERS[plan]:
            raise HTTPException(422, '无效套餐')
        if plan == 'Free':
            amount, grant, annual = Decimal(0), Decimal(0), False
        else:
            amount = Decimal(credits) / RATE
            if annual:
                amount *= Decimal('0.82' if plan == 'Pro' else '0.79') * 12
            grant = Decimal(credits)
    else:
        raise HTTPException(422, '无效订单类型')
    existing = conn.execute('SELECT * FROM billing_orders WHERE user_id=%s AND idempotency_key=%s', (user_id, key)).fetchone()
    if existing:
        if (existing['kind'], existing['plan'], existing['plan_credits'], existing['annual'], existing['amount_usd']) != (kind, plan, credits, annual, amount):
            raise HTTPException(409, '同一订单标识不能用于不同商品')
        return existing
    # Prevent overlapping prepaid subscriptions from granting duplicate periods.
    if kind == 'subscription' and plan != 'Free':
        current = conn.execute('SELECT paid_until FROM account_profiles WHERE user_id=%s', (user_id,)).fetchone()
        if current['paid_until'] and current['paid_until'] > datetime.now(timezone.utc):
            raise HTTPException(409, '当前套餐周期尚未结束；可直接充值积分，或取消套餐后选择新套餐')
    return conn.execute("""INSERT INTO billing_orders(id,user_id,idempotency_key,kind,plan,plan_credits,annual,amount_usd,credits)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (uuid.uuid4(), user_id, key, kind, plan, credits, annual, amount, grant)).fetchone()


def pay_order(conn, user_id, order_id, outcome='success'):
    conn.execute('SELECT user_id FROM account_profiles WHERE user_id=%s FOR UPDATE', (user_id,))
    order = conn.execute('SELECT * FROM billing_orders WHERE id=%s AND user_id=%s FOR UPDATE', (order_id, user_id)).fetchone()
    if not order:
        raise HTTPException(404, '订单不存在')
    if order['status'] == 'paid':
        return order
    if order['status'] != 'pending':
        raise HTTPException(409, '订单已关闭，请重新创建订单')
    if order['expires_at'] <= datetime.now(timezone.utc):
        raise HTTPException(409, '订单已过期，请重新创建订单')
    if outcome != 'success':
        return conn.execute('UPDATE billing_orders SET status=%s WHERE id=%s RETURNING *', ('cancelled' if outcome == 'cancel' else 'failed', order_id)).fetchone()
    now = datetime.now(timezone.utc)
    if order['kind'] == 'subscription':
        current = conn.execute('SELECT paid_until FROM account_profiles WHERE user_id=%s', (user_id,)).fetchone()
        if order['plan'] != 'Free' and current['paid_until'] and current['paid_until'] > now:
            raise HTTPException(409, '已有生效套餐，未重复支付或到账')
        until = now.replace(year=now.year + 1, day=min(now.day, calendar.monthrange(now.year + 1, now.month)[1])) if order['annual'] else next_month(now)
        conn.execute("UPDATE account_profiles SET plan=%s,plan_credits=%s,plan_interval=%s,paid_until=%s,next_grant_at=%s WHERE user_id=%s",
                     (order['plan'], order['plan_credits'], 'year' if order['annual'] else 'month', until if order['plan'] != 'Free' else None, next_month(now) if order['annual'] else None, user_id))
    ledger(conn, user_id, order['credits'], 'topup' if order['kind'] == 'topup' else 'subscription', '模拟支付充值到账' if order['kind'] == 'topup' else f"{order['plan']} 套餐积分到账", order_id=order_id, metadata={'payment_mode': 'simulation', 'amount_usd': str(order['amount_usd']), 'credits_per_usd': str(RATE), 'annual': order['annual']})
    return conn.execute("UPDATE billing_orders SET status='paid',paid_at=NOW() WHERE id=%s RETURNING *", (order_id,)).fetchone()
