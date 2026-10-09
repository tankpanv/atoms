"""Invite attribution and one-time rewards recorded in the real billing ledger."""
import secrets
from decimal import Decimal
from auth import connection, current_user
from fastapi import APIRouter, Depends, HTTPException
from billing import ledger
router = APIRouter(prefix='/api/account/referrals', tags=['account'])

def init_referrals(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS referral_codes(user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,code TEXT UNIQUE NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS referrals(invitee UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        inviter UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE, first_message_at TIMESTAMPTZ,published_at TIMESTAMPTZ,
        inviter_rewarded BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),CHECK(invitee<>inviter))''')

@router.get('')
def info(user=Depends(current_user)):
    with connection() as conn:
        conn.execute('INSERT INTO referral_codes(user_id,code) VALUES(%s,%s) ON CONFLICT(user_id) DO NOTHING', (user['id'],secrets.token_hex(12)))
        code = conn.execute('SELECT code FROM referral_codes WHERE user_id=%s', (user['id'],)).fetchone()['code']
        rewarded = conn.execute("SELECT COUNT(*) AS count FROM referrals WHERE inviter=%s AND inviter_rewarded AND date_trunc('month',published_at)=date_trunc('month',NOW())", (user['id'],)).fetchone()['count']
        return {'code':code,'rewarded':rewarded,'monthly_limit':10,'reward':10}

def attribute(conn,invitee,code):
    if not code:
        return
    row = conn.execute('SELECT user_id FROM referral_codes WHERE code=%s', (code,)).fetchone()
    if not row or row['user_id']==invitee:
        raise HTTPException(422,'邀请链接无效')
    conn.execute('INSERT INTO referrals(invitee,inviter) VALUES(%s,%s) ON CONFLICT DO NOTHING', (invitee,row['user_id']))

def message_reward(conn, user):
    referral = conn.execute('SELECT * FROM referrals WHERE invitee=%s FOR UPDATE', (user['id'],)).fetchone()
    if not referral or referral['first_message_at']:
        return
    from account import ensure_profile
    ensure_profile(conn,user)
    ledger(conn,user['id'],Decimal(10),'referral_signup','邀请注册并首次构建奖励',metadata={'inviter':str(referral['inviter'])})
    conn.execute('UPDATE referrals SET first_message_at=NOW() WHERE invitee=%s', (user['id'],))

def publish_reward(conn, user):
    referral = conn.execute('SELECT * FROM referrals WHERE invitee=%s FOR UPDATE', (user['id'],)).fetchone()
    if not referral or referral['published_at'] or not referral['first_message_at']:
        return
    from account import ensure_profile
    inviter = conn.execute('SELECT id,email FROM users WHERE id=%s', (referral['inviter'],)).fetchone()
    ensure_profile(conn,inviter)
    conn.execute('SELECT credits FROM account_profiles WHERE user_id=%s FOR UPDATE', (inviter['id'],))
    count = conn.execute("SELECT COUNT(*) AS count FROM referrals WHERE inviter=%s AND inviter_rewarded AND date_trunc('month',published_at)=date_trunc('month',NOW())", (inviter['id'],)).fetchone()['count']
    rewarded = count < 10
    if rewarded:
        ledger(conn,inviter['id'],Decimal(10),'referral_publish','受邀用户首次发布奖励',metadata={'invitee':str(user['id'])})
    conn.execute('UPDATE referrals SET published_at=NOW(),inviter_rewarded=%s WHERE invitee=%s', (rewarded,user['id']))
