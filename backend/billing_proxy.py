"""Trusted model gateway: reserve first, consume upstream usage, settle exactly once."""
import asyncio
import hashlib
import json
import os
import uuid

import httpx
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from auth import connection
from billing import reserve_request, settle_request, reject_request, grant_due
from coding_runtime import ModelGateway, ModelContextOverflow, ModelProviderError
from model_catalog import canonical_model_id

active_requests: dict[uuid.UUID, asyncio.Task] = {}


def valid_usage(usage):
    return all(isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
               and usage[key] >= 0 for key in ('prompt_tokens', 'completion_tokens'))


def mark_unknown(request_id, error):
    with connection() as conn:
        conn.execute("UPDATE billing_requests SET status='unknown',error=%s,updated_at=NOW() WHERE id=%s AND status='reserved'", (str(error)[:1200], request_id))


async def execute_request(request_id, payload):
    gateway = ModelGateway(payload['model'])
    seen_id = None
    latest_usage = None
    seen_model = None

    def metadata(data):
        nonlocal seen_id, latest_usage, seen_model
        generation = data.get('id')
        usage = data.get('usage')
        if generation and generation != seen_id:
            with connection() as conn:
                conn.execute('UPDATE billing_requests SET generation_id=%s,updated_at=NOW() WHERE id=%s', (generation, request_id))
            seen_id = generation
        if usage:
            latest_usage = usage
            with connection() as conn:
                conn.execute('UPDATE billing_requests SET usage=%s,updated_at=NOW() WHERE id=%s', (Jsonb(usage), request_id))
        if data.get('model') and data['model'] != seen_model:
            with connection() as conn:
                conn.execute('UPDATE billing_requests SET provider_model=%s WHERE id=%s', (data['model'], request_id))
            seen_model = data['model']
    gateway.on_metadata = metadata
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=15)) as client:
            result = await gateway._stream(client, {**payload, 'stream': True, 'stream_options': {'include_usage': True}})
        with connection() as conn:
            # Persist a complete response before accounting. Missing usage must
            # not discard a valid answer or trigger another paid generation.
            conn.execute('UPDATE billing_requests SET response=%s,attempt_finished=TRUE,updated_at=NOW() WHERE id=%s AND project_id IS NOT NULL', (Jsonb(result),request_id))
        usage = result.get('usage') or {}
        if valid_usage(usage):
            with connection() as conn:
                settle_request(conn, request_id, usage, result)
        else:
            mark_unknown(request_id, '模型响应已保存，用量等待真实对账')
        return result
    except Exception as exc:
        # No HTTP request body has reached the provider on these failures.
        # Read/write timeouts and truncated responses remain ambiguous: absence
        # of a generation ID alone is NOT proof that inference never started.
        not_sent = (isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                    and seen_id is None and latest_usage is None)
        terminal = not_sent or isinstance(exc, ModelProviderError) or isinstance(exc, httpx.HTTPStatusError)
        retry_safe = not_sent or (isinstance(exc, ModelProviderError) and exc.retryable) or (
            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {429,500,502,503,504,524,529})
        with connection() as conn:
            conn.execute('UPDATE billing_requests SET attempt_finished=attempt_finished OR %s,retry_safe=%s,error=%s WHERE id=%s', (terminal,retry_safe,str(exc)[:1200],request_id))
        # Even a failed/truncated stream is charged when real usage was reported.
        if latest_usage and valid_usage(latest_usage):
            with connection() as conn:
                settle_request(conn, request_id, latest_usage)
                conn.execute('UPDATE billing_requests SET error=%s WHERE id=%s', (str(exc)[:1200], request_id))
        elif not_sent or (not seen_id and (isinstance(exc, ModelContextOverflow) or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500) or (isinstance(exc, RuntimeError) and '请求失败 (4' in str(exc)))):
            with connection() as conn:
                reject_request(conn, request_id, str(exc))
        else:
            mark_unknown(request_id, exc)
        if retry_safe:
            raise HTTPException(503, {'code':'MODEL_ATTEMPT_FAILED','retry_safe':True,
                'message':'上游模型服务暂时失败，将保留上下文重试；本次用量独立对账'}) from exc
        raise HTTPException(502, f'模型调用失败：{str(exc)[:500]}') from exc


async def proxy_model(project_id, payload):
    try:
        request_id = uuid.UUID(payload.pop('_billing_request'))
        job_id = uuid.UUID(payload.pop('_billing_job'))
        stage = payload.pop('_billing_stage')
        if stage not in ('UNDERSTAND', 'EXPLORE', 'PLAN', 'IMPLEMENT', 'STABILIZE', 'TEST', 'REVIEW', 'COMPLETE', 'COMPACT'):
            raise ValueError('invalid stage')
        # Only supported upstream parameters; no callback URLs or user-supplied cost.
        allowed = {'model', 'messages', 'max_tokens', 'stream', 'tools', 'tool_choice', 'reasoning', 'reasoning_effort', 'session_id'}
        if set(payload) - allowed or not isinstance(payload['messages'], list):
            raise ValueError('invalid request')
        if 'session_id' in payload and (not isinstance(payload['session_id'], str) or
                not payload['session_id'].startswith(f'atoms:{project_id.hex}:') or len(payload['session_id']) > 256):
            raise ValueError('invalid session identity')
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, '无效模型计费请求')
    payload = {**payload, 'model': canonical_model_id(payload['model'])}
    with connection() as conn:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row, created = reserve_request(conn, request_id, project_id, job_id, payload['model'], stage, payload['max_tokens'], digest)
    if not created:
        if row['response']:
            return row['response']
        if row['retry_safe']:
            raise HTTPException(503, {'code':'MODEL_ATTEMPT_FAILED','retry_safe':True,
                'message':'此前上游调用已明确失败，可以发起新的尝试'})
        if row['status'] == 'rejected':
            raise HTTPException(502, row['error'])
        if request_id not in active_requests:
            raise HTTPException(503, {'code':'MODEL_REQUEST_PENDING','retry_safe':False,
                'message':'此前模型请求状态正在核实，等待原请求结果，避免重复调用和扣费'})
    if created:
        task = asyncio.create_task(execute_request(request_id, payload))
        active_requests[request_id] = task
        task.add_done_callback(lambda done: active_requests.pop(request_id, None))
        # Retrieve exceptions even when the worker disconnects or cancels its request.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return await asyncio.shield(active_requests[request_id])


async def reconcile_once(request_ids=None):
    # Scoped invocation supports controlled repair/integration tests without
    # touching unrelated accounts or requests. The scheduler reconciles all.
    if request_ids is None:
        with connection() as conn:
            due = conn.execute('SELECT user_id FROM account_profiles WHERE next_grant_at<=NOW() OR paid_until<=NOW() ORDER BY user_id').fetchall()
            for account in due:
                grant_due(conn, account['user_id'])
    with connection() as conn:
        rows = conn.execute("""SELECT * FROM billing_requests WHERE (status IN ('reserved','unknown')
            OR (status='settled' AND response IS NULL AND NOT attempt_finished AND generation_id IS NOT NULL))
            AND updated_at<NOW()-INTERVAL '30 seconds' AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[]))
            ORDER BY created_at LIMIT 100""", (request_ids,request_ids)).fetchall()
    for row in rows:
        if row['id'] in active_requests:
            continue
        # Repair the old, narrowly identifiable pre-connect classification.
        # Never apply this migration to lost responses, any usage or an ID.
        if (row['error'] == 'All connection attempts failed' and not row['generation_id']
                and not row['usage'] and row['response'] is None and not row['attempt_finished']):
            with connection() as conn:
                reject_request(conn, row['id'], row['error'])
                conn.execute('UPDATE billing_requests SET attempt_finished=TRUE,retry_safe=TRUE WHERE id=%s', (row['id'],))
            continue
        usage = row['usage']
        if row['generation_id'] and (not valid_usage(usage) or (not row['response'] and not row['attempt_finished'])):
            try:
                base_url = os.getenv('AI_BASE_URL', 'https://openrouter.ai/api/v1').rstrip('/')
                # Only OpenRouter exposes this delayed usage endpoint. Other
                # OpenAI-compatible sources may not implement it.
                if base_url.endswith('/chat/completions'):
                    base_url = base_url[:-len('/chat/completions')]
                if 'openrouter.ai' not in base_url:
                    continue
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(base_url + '/generation',
                        params={'id': row['generation_id']}, headers={'Authorization': 'Bearer ' + os.getenv('AI_API_KEY', '')})
                    response.raise_for_status()
                    data = response.json()['data']
                if not row['attempt_finished'] and (data.get('finish_reason') or data.get('cancelled')):
                    # A previously lost connection is now confirmed terminal.
                    # With no locally delivered response/tools, a fresh attempt
                    # is safe; accounting and model generation stay separate.
                    with connection() as conn:
                        conn.execute('UPDATE billing_requests SET attempt_finished=TRUE,retry_safe=(response IS NULL),updated_at=NOW() WHERE id=%s', (row['id'],))
                if (row['attempt_finished'] or data.get('finish_reason') or data.get('cancelled')) and data.get('native_tokens_prompt') is not None and data.get('native_tokens_completion') is not None:
                    usage = {'prompt_tokens': data['native_tokens_prompt'], 'completion_tokens': data['native_tokens_completion'], 'cost': data.get('total_cost'), 'reconciled': True}
            except (httpx.HTTPError, ValueError, KeyError):
                continue
        if valid_usage(usage):
            with connection() as conn:
                settle_request(conn, row['id'], usage)
        elif row['status'] == 'reserved':
            mark_unknown(row['id'], '服务重启时用量未完整返回，冻结额度等待真实用量对账')


async def reconcile_loop():
    while True:
        try:
            await reconcile_once()
        except Exception as exc:
            print(f'Billing reconciliation failed: {type(exc).__name__}', flush=True)
        await asyncio.sleep(15)
