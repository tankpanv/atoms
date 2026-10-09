"""Issue a local credit code through the administrator's CLI, never a public API."""
import argparse
import hashlib
import secrets
from decimal import Decimal

from account import connection, init_account_db


def main():
    parser = argparse.ArgumentParser(description="创建一次性积分兑换码")
    parser.add_argument("--credits", required=True, type=Decimal)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    if not args.credits.is_finite() or args.credits <= 0 or args.days < 1:
        parser.error("credits 必须大于零，days 必须至少为 1")
    code = secrets.token_urlsafe(24)
    with connection() as conn:
        init_account_db(conn)
        conn.execute("INSERT INTO account_credit_codes(code_hash,credits,expires_at) VALUES(%s,%s,NOW()+make_interval(days=>%s))", (hashlib.sha256(code.encode()).hexdigest(), args.credits, args.days))
    print(code)


if __name__ == '__main__':
    main()
