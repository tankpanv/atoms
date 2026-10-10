"""Model routing, bounded code retrieval, and checked source patches."""

from __future__ import annotations

import difflib
import copy
import asyncio
import json
import hashlib
import logging
import os
import re
import time
from urllib.parse import urlparse
import uuid
from enum import Enum
from pathlib import Path
from typing import Callable

import httpx
from billing_context import active_job

logger = logging.getLogger(__name__)
# Shared by gateways in the trusted service, scoped to credentials and model.
# Workers never receive the dedicated upstream credentials.
_openai_unavailable: dict[tuple[str, str, str], float] = {}
OPENAI_RECOVERY_SECONDS = 300


class AgentState(str, Enum):
    COMPACT = "COMPACT"
    UNDERSTAND = "UNDERSTAND"
    EXPLORE = "EXPLORE"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    TEST = "TEST"
    REVIEW = "REVIEW"
    STABILIZE = "STABILIZE"
    PREVIEW_READY = "PREVIEW_READY"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


# Provider context windows have grown well beyond the old 4k/8k defaults.  A
# large output allowance does not force a long answer: the model still stops
# when it has completed the requested JSON or tool call.  These are stage
# ceilings so short structured phases do not reserve the full 128k budget,
# while implementation can still return a large coherent patch/tool batch.
OUTPUT_TOKEN_CAP = 128_000
STAGE_OUTPUT_TOKEN_CAPS = {
    AgentState.PLAN: 128_000,
    AgentState.IMPLEMENT: 128_000,
    AgentState.STABILIZE: 128_000,
    AgentState.TEST: 65_536,
    AgentState.REVIEW: 65_536,
    AgentState.UNDERSTAND: 65_536,
    AgentState.EXPLORE: 65_536,
    AgentState.COMPACT: 32_768,
    AgentState.COMPLETE: 32_768,
}


def model_output_limit(model: str, state: AgentState, requested: int | None = None) -> int:
    """Return a provider-aware output ceiling for one model phase.

    ``requested`` is treated as a lower-level caller preference only; legacy
    callers asking for 8k must not silently reintroduce the historical global
    cap.  The provider context remains the final hard boundary.
    """
    from model_catalog import catalog
    entry = next((item for item in catalog() if item['id'] == model), None)
    context = int(entry['context']) if entry else 128_000
    configured = os.getenv('AGENT_MAX_OUTPUT_TOKENS', str(OUTPUT_TOKEN_CAP))
    try:
        configured = max(4096, int(configured))
    except ValueError:
        configured = OUTPUT_TOKEN_CAP
    stage_cap = STAGE_OUTPUT_TOKEN_CAPS.get(state, 32_768)
    # A model cannot spend its whole context on completion when the prompt is
    # already present; retain a small provider-safe reserve for input.
    provider_cap = max(4096, context - 4096)
    return min(configured, stage_cap, provider_cap)


class AgentStateMachine:
    transitions = {
        None: {AgentState.UNDERSTAND, AgentState.IMPLEMENT},
        AgentState.UNDERSTAND: {AgentState.EXPLORE, AgentState.IMPLEMENT},
        AgentState.EXPLORE: {AgentState.PLAN, AgentState.IMPLEMENT},
        AgentState.PLAN: {AgentState.IMPLEMENT},
        AgentState.IMPLEMENT: {AgentState.TEST},
        AgentState.TEST: {AgentState.IMPLEMENT, AgentState.REVIEW, AgentState.COMPLETE},
        AgentState.REVIEW: {AgentState.IMPLEMENT, AgentState.COMPLETE},
    }

    def __init__(self):
        self.current: AgentState | None = None

    def transition(self, next_state: AgentState):
        if next_state == AgentState.STABILIZE and self.current in (AgentState.IMPLEMENT, AgentState.TEST, AgentState.REVIEW, AgentState.STABILIZE):
            self.current = next_state
            return
        if self.current == AgentState.STABILIZE and next_state in (AgentState.TEST, AgentState.PREVIEW_READY):
            self.current = next_state
            return
        if self.current == AgentState.TEST and next_state == AgentState.PREVIEW_READY:
            self.current = next_state
            return
        if next_state in (AgentState.FAILED, AgentState.STOPPED):
            self.current = next_state
            return
        if next_state not in self.transitions.get(self.current, set()):
            raise ValueError(f"无效的 Agent 阶段转换：{self.current} → {next_state}")
        self.current = next_state


class ModelProviderError(RuntimeError):
    """An explicit terminal provider error; no partial tool calls may execute."""
    def __init__(self, error):
        self.error = error
        code = error.get('code') if isinstance(error, dict) else None
        self.retryable = str(code) in {'429', '500', '502', '503', '504', '524', '529'}
        super().__init__(f'模型流式返回错误：{str(error)[:500]}')


class ModelTemporaryError(RuntimeError):
    """Recoverable availability failure, distinct from invalid config/auth.

    pending=True preserves the original request identity until reconciliation;
    only trusted, explicitly terminated attempts may get a fresh identity.
    """
    def __init__(self, message, *, pending=False):
        self.pending = pending
        super().__init__(message)


class ModelGateway:
    def __init__(self, selected_model: str, on_usage: Callable[[str, dict], None] | None = None,
                 on_progress: Callable[[], None] | None = None):
        self.selected_model = selected_model
        self.on_usage = on_usage
        self.on_progress = on_progress
        self.on_metadata = None
        self.on_attempt_start = None
        self.on_attempt_error = None
        self.dedicated_client = None
        self.session = None
        self.cache_scope = os.getenv('PROJECT_ID', '')
        # AI_BASE_URL is an OpenAI-compatible API root (for example
        # ``https://openrouter.ai/api/v1`` or ``https://api.quickrouter.ai/v1``).
        # Accept a full chat-completions URL as well, because older local .env
        # files used that form, but never append /chat/completions twice.
        self.base_url = os.getenv("AI_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self.key = os.getenv("AI_API_KEY", "").strip()
        if not self.key and not os.getenv('WORKER_TOKEN'):
            raise RuntimeError("未配置 AI_API_KEY，无法运行代码智能体")

    def model_for(self, state: AgentState) -> str:
        return os.getenv(f"AI_{state.value}_MODEL", "").strip() or (os.getenv("AI_IMPLEMENT_MODEL", "").strip() if state == AgentState.STABILIZE else "") or self.selected_model

    def outbound_model(self, model: str) -> str:
        """Convert our canonical provider/model ID to a source's wire ID."""
        host = (urlparse(self.base_url).hostname or "").lower()
        # QuickRouter's documented endpoint accepts bare model names, while
        # our model_list and billing layer intentionally use provider/model IDs.
        if host == "api.quickrouter.ai" and "/" in model:
            return model.split("/", 1)[1]
        return model

    async def _stream(self, client: httpx.AsyncClient, payload: dict) -> dict:
        if os.getenv('WORKER_TOKEN'):
            return await self._stream_once(client, payload)
        base_url = os.getenv('OPENAI_BASE_URL', '').strip().rstrip('/')
        key = os.getenv('OPENAI_API_KEY', '').strip()
        model = payload.get('model', '')
        route_key = (base_url, hashlib.sha256(key.encode()).hexdigest(), model)
        preferred = bool(base_url and key and model.startswith('openai/gpt'))

        async def attempt(source, endpoint, credential, dedicated=False):
            # Accounting hooks run outside the provider error handler: a failed
            # reservation must never mark a healthy provider unavailable.
            if self.on_attempt_start:
                await self.on_attempt_start(source, endpoint)
            try:
                upstream_client = self.dedicated_client if dedicated and self.dedicated_client else client
                result = await self._stream_once(upstream_client, payload, base_url=endpoint,
                                                 key=credential, dedicated=dedicated)
                result['_provider_source'] = source
                return result
            except Exception as exc:
                if self.on_attempt_error:
                    await self.on_attempt_error(exc)
                raise

        if preferred and time.monotonic() >= _openai_unavailable.get(route_key, 0):
            for number in range(3):
                try:
                    result = await attempt('openai', base_url, key, dedicated=True)
                    _openai_unavailable.pop(route_key, None)
                    return result
                except (RuntimeError, httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
                    # Context overflow needs context compaction, not another source.
                    if isinstance(exc, ModelContextOverflow):
                        raise
                    logger.warning('Dedicated OpenAI model %s attempt %d/3 failed (%s)',
                                   model, number + 1, type(exc).__name__)
                    if self.on_progress:
                        self.on_progress()
                    if number < 2:
                        await asyncio.sleep(2 ** number)
            _openai_unavailable[route_key] = time.monotonic() + OPENAI_RECOVERY_SECONDS
            logger.warning('Dedicated OpenAI model %s unavailable for %ds; using aggregate source',
                           model, OPENAI_RECOVERY_SECONDS)
        return await attempt('aggregate', self.base_url, self.key)

    async def _stream_once(self, client: httpx.AsyncClient, payload: dict, *,
                           base_url: str | None = None, key: str | None = None,
                           dedicated: bool = False) -> dict:
        base_url = self.base_url if base_url is None else base_url
        key = self.key if key is None else key
        payload = copy.deepcopy(payload)
        if dedicated:
            from agent_session import portable_messages
            payload['model'] = payload['model'].split('/', 1)[1]
            if 'max_tokens' in payload:
                payload['max_completion_tokens'] = payload.pop('max_tokens')
            reasoning = payload.pop('reasoning', None)
            if isinstance(reasoning, dict) and reasoning.get('effort'):
                payload['reasoning_effort'] = reasoning['effort']
            # The aggregate QuickRouter path disables reasoning for tools.
            # This GPT source accepts low/medium/high/xhigh/max, so translate
            # the aggregate's none/minimal setting at this boundary only.
            if payload.get('reasoning_effort') in {'none', 'minimal'}:
                payload['reasoning_effort'] = 'low'
            payload.pop('session_id', None)
            payload['messages'] = portable_messages(payload['messages'])
            payload.setdefault('stream_options', {'include_usage': True})
        # Keep the canonical ID through worker/billing; only direct upstream
        # requests use the provider's wire-format model name.
        if not dedicated and not os.getenv('WORKER_TOKEN'):
            payload = {**payload, "model": self.outbound_model(payload.get("model", ""))}
        # Apply provider compatibility at the final network boundary. Agent
        # calls pass through the billing proxy, so adapting only in chat() on
        # project workers is insufficient: the proxy's own ModelGateway sends
        # the actual provider request.
        if not dedicated and (urlparse(base_url).hostname or '').lower() == 'api.quickrouter.ai':
            payload = dict(payload)
            reasoning = payload.pop('reasoning', None)
            if payload.get('tools'):
                # QuickRouter requires this exact setting for function tools.
                payload['reasoning_effort'] = 'none'
            elif reasoning is not None:
                # QuickRouter's Chat Completions endpoint does not accept
                # OpenRouter's `reasoning` object. Map its effort if present.
                effort = reasoning.get('effort') if isinstance(reasoning, dict) else None
                if effort in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
                    payload['reasoning_effort'] = effort
                else:
                    payload.pop('reasoning_effort', None)
        headers = {"Authorization": f"Bearer {key}", "HTTP-Referer": "http://localhost:5173",
                   "X-OpenRouter-Title": "Atoms Coding Agent"}
        if dedicated:
            headers = {'Authorization': f'Bearer {key}'}
        url = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        if os.getenv('WORKER_TOKEN'):
            if not active_job.get():
                raise RuntimeError('模型调用缺少计费任务，拒绝绕过计费')
            url = f"http://agent-service:9001/projects/{os.environ['PROJECT_ID']}/model"
            headers = {'X-Worker-Token': os.environ['WORKER_TOKEN']}
            payload = {**payload, '_billing_job': active_job.get()}
        if not dedicated and not os.getenv('WORKER_TOKEN') and 'openrouter.ai' in base_url:
            payload = {**payload, 'messages': [dict(m) for m in payload['messages']]}
            # Anthropic requires explicit breakpoints; other supported providers
            # use automatic prefix caching. Never cache generated responses locally.
            if payload['model'].startswith('anthropic/'):
                indexes = sorted({0, min(1, len(payload['messages']) - 1), len(payload['messages']) - 1})
                for index in indexes:
                    if index < 0:
                        continue
                    message = payload['messages'][index]
                    if isinstance(message.get('content'), str) and message['content']:
                        message['content'] = [{'type': 'text', 'text': message['content'],
                                               'cache_control': {'type': 'ephemeral'}}]
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode(errors="replace")[:500]
                if context_overflow(body):
                    raise ModelContextOverflow(body)
                if response.status_code in {429, 500, 502, 503, 504, 524, 529}:
                    raise httpx.HTTPStatusError(body, request=response.request, response=response)
                raise RuntimeError(f"模型 {payload['model']} 请求失败 ({response.status_code}): {body}")
            if 'application/json' in response.headers.get('content-type', ''):
                data = json.loads(await response.aread())
                if self.on_metadata:
                    self.on_metadata(data)
                if data.get('error'):
                    if context_overflow(str(data['error'])):
                        raise ModelContextOverflow(str(data['error']))
                    raise ModelProviderError(data['error'])
                if not data.get('choices') or not isinstance(data['choices'][0].get('message'), dict):
                    raise httpx.RemoteProtocolError('模型返回无效的 Chat Completions 响应')
                return data
            content, reasoning, calls, usage, details = [], [], {}, {}, []
            details_seen = False
            finish_reason = None
            generation_id = None
            response_model = None
            last_progress = time.monotonic()
            async for line in response.aiter_lines():
                if self.on_progress and time.monotonic() - last_progress > 15:
                    self.on_progress()
                    last_progress = time.monotonic()
                if not line.startswith('data:'):
                    continue
                value = line[5:].strip()
                if value == '[DONE]':
                    break
                data = json.loads(value)
                generation_id = data.get('id') or generation_id
                response_model = data.get('model') or response_model
                if self.on_metadata and (data.get('id') or data.get('usage')):
                    self.on_metadata(data)
                if data.get('error'):
                    if context_overflow(str(data['error'])):
                        raise ModelContextOverflow(str(data['error']))
                    raise ModelProviderError(data['error'])
                if data.get('usage'):
                    usage = data['usage']
                for choice in data.get('choices') or []:
                    if choice.get('index', 0) != 0:
                        continue
                    if choice.get('finish_reason'):
                        finish_reason = choice['finish_reason']
                    delta = choice.get('delta') or {}
                    if delta.get('content'):
                        content.append(delta['content'])
                    if delta.get('reasoning') or delta.get('reasoning_content'):
                        reasoning.append(delta.get('reasoning') or delta['reasoning_content'])
                    if 'reasoning_details' in delta:
                        details_seen = True
                    for detail in delta.get('reasoning_details') or []:
                        # Reassemble contiguous streamed text/summary deltas, retaining
                        # signed metadata and opaque encrypted blocks in their order.
                        previous = details[-1] if details else None
                        kind = detail.get('type')
                        field = {'reasoning.text': 'text', 'reasoning.summary': 'summary'}.get(kind)
                        if (field and previous and previous.get('type') == kind and
                                not (previous.get('id') and detail.get('id') and previous['id'] != detail['id'])):
                            previous[field] = (previous.get(field) or '') + (detail.get(field) or '')
                            for key, value in detail.items():
                                if key != field and value is not None and not previous.get(key):
                                    previous[key] = value
                        else:
                            details.append(dict(detail))
                    for piece in delta.get('tool_calls') or []:
                        index = piece.get('index', 0)
                        call = calls.setdefault(index, {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                        if piece.get('id'):
                            call['id'] = piece['id']
                        function = piece.get('function') or {}
                        if function.get('name'):
                            call['function']['name'] += function['name']
                        if function.get('arguments'):
                            call['function']['arguments'] += function['arguments']
            if finish_reason is None:
                raise httpx.RemoteProtocolError("模型流提前结束，不能执行不完整的工具调用")
            message = {'role': 'assistant', 'content': ''.join(content)}
            if calls:
                message['tool_calls'] = [calls[index] for index in sorted(calls)]
            if reasoning and not details:
                message['reasoning'] = ''.join(reasoning)
            if details_seen:
                message['reasoning_details'] = details
            return {'id': generation_id, 'model': response_model, 'choices': [{'message': message, 'finish_reason': finish_reason}], 'usage': usage}

    async def chat(self, client: httpx.AsyncClient, state: AgentState, messages: list[dict],
                   tools: list[dict] | None = None, max_tokens: int | None = None,
                   reasoning: dict | None = None) -> dict:
        pending = (self.session.state.get('pending_model') or {}) if self.session and os.getenv('WORKER_TOKEN') else {}
        if pending.get('job_id') != active_job.get() or not pending.get('payload'):
            pending = {}
        if pending and pending['payload'].get('_billing_stage') != state.value:
            # Reconcile the exact old request before preflighting the NEW stage.
            # Resume instructions may be large; they cannot prevent settlement
            # of a valid request or authorize executing its old proposed tools.
            original = copy.deepcopy(pending['payload'])
            old_stage = AgentState(original['_billing_stage'])
            recovered = await self.chat(client, old_stage, copy.deepcopy(original['messages']),
                                        tools=original.get('tools'),
                                        max_tokens=original.get('max_tokens') or model_output_limit(self.model_for(old_stage), old_stage),
                                        reasoning=original.get('reasoning'))
            self.session.save('recovered_model_response', {
                'request_id': original['_billing_request'], 'stage': original['_billing_stage'],
                'response': recovered, 'proposed_tools_executed': False})
            pending = {}
        if max_tokens is None:
            max_tokens = model_output_limit(self.model_for(state), state)
        from agent_session import estimate_tokens
        from model_catalog import catalog
        context = next((int(item['context']) for item in catalog() if item['id'] == self.model_for(state)), 128000)
        available = context - estimate_tokens(messages, tools) - 4000
        if available < 512 and not pending:
            raise ModelContextOverflow('当前输入超过模型上下文可用范围，需压缩活动上下文后重试。')
        if not pending:
            max_tokens = min(max_tokens, available)
        payload: dict = {"model": self.model_for(state), "messages": messages, "max_tokens": max_tokens, "stream": True}
        if tools:
            payload.update({"tools": tools, "tool_choice": "auto"})
        if reasoning is not None:
            payload['reasoning'] = reasoning
        elif state == AgentState.COMPACT and 'openrouter.ai' in self.base_url:
            payload['reasoning'] = {'effort': 'low'}
        # QuickRouter's Chat Completions endpoint rejects reasoning with
        # function tools. Its contract requires reasoning_effort=none for
        # tool calls; keep reasoning enabled for ordinary text requests.
        if tools and (urlparse(self.base_url).hostname or '').lower() == 'api.quickrouter.ai':
            payload.pop('reasoning', None)
            payload['reasoning_effort'] = 'none'
        if 'openrouter.ai' in self.base_url and self.cache_scope:
            payload['session_id'] = (self.session.cache_identity(state.value) if self.session else
                                     f'atoms:{self.cache_scope}:planning')
        if os.getenv('WORKER_TOKEN'):
            identifier = (pending['payload']['_billing_request'] if pending else
                          self.session.request_id(payload, state.value, active_job.get()) if self.session else str(uuid.uuid4()))
            payload.update({'_billing_request': identifier, '_billing_stage': state.value})
            if pending:
                # Resuming a job adds checkpoint instructions to its context.
                # They must not create a NEW generation when the old request
                # may still be in flight. Replay its exact payload/identity.
                payload = copy.deepcopy(pending['payload'])
                messages[:] = copy.deepcopy(payload['messages'])
                max_tokens = payload.get('max_tokens', max_tokens)

        def pending_checkpoint(clear=False):
            if self.session and os.getenv('WORKER_TOKEN'):
                if clear:
                    self.session.state.pop('pending_model', None)
                else:
                    self.session.state['pending_model'] = {'job_id': active_job.get(), 'payload': copy.deepcopy(payload)}
                self.session.save('model_request_checkpoint', {'stage':state.value, 'pending':not clear})
        started = time.monotonic()
        recovered_route = False
        for attempt in range(3):
            try:
                pending_checkpoint()
                data = await self._stream(client, payload)
                break
            except (RuntimeError, httpx.HTTPStatusError) as exc:
                # Rotate billing identity ONLY after the trusted gateway confirms
                # that the provider attempt terminated, never on a lost connection.
                safe_retry = isinstance(exc, ModelProviderError) and exc.retryable
                pending_request = False
                if isinstance(exc, httpx.HTTPStatusError):
                    try:
                        detail = exc.response.json().get('detail', {})
                        safe_retry = isinstance(detail, dict) and detail.get('code') == 'MODEL_ATTEMPT_FAILED' and detail.get('retry_safe') is True
                        pending_request = isinstance(detail, dict) and detail.get('code') == 'MODEL_REQUEST_PENDING'
                    except (ValueError, AttributeError):
                        pass
                if safe_retry:
                    if attempt == 2:
                        pending_checkpoint(clear=True)
                        raise ModelTemporaryError('模型服务暂时不可用，已保留上下文，正在自动恢复。') from exc
                    if os.getenv('WORKER_TOKEN'):
                        clean = {k:v for k,v in payload.items() if not k.startswith('_billing_')}
                        payload['_billing_request'] = (self.session.request_id(clean, state.value, active_job.get(), renew=True)
                            if self.session else str(uuid.uuid4()))
                    if self.on_progress:
                        self.on_progress()
                    await asyncio.sleep(2 ** attempt)
                    continue
                if pending_request and attempt == 2:
                    raise ModelTemporaryError('此前模型请求结果正在核实，保留原请求并等待恢复。', pending=True) from exc
                if pending_request:
                    if self.on_progress:
                        self.on_progress()
                    # Give the 15-second reconciliation loop a real opportunity
                    # to resolve the prior generation. Poll the SAME identity.
                    await asyncio.sleep(15 * (attempt + 1))
                    continue
                encrypted = any(d.get('type') == 'reasoning.encrypted'
                                for m in payload['messages'] for d in (m.get('reasoning_details') or []) if isinstance(d, dict))
                text = str(exc).lower()
                if not recovered_route and encrypted and 'region' in text and 'encrypted' in text:
                    # Opaque reasoning pins an endpoint and cannot migrate. Keep
                    # all observable messages/tool receipts, never replay tools,
                    # and start a fresh routing identity exactly once.
                    from agent_session import portable_messages
                    recovered_route = True
                    messages[:] = portable_messages(messages)
                    payload['messages'] = messages
                    if self.session:
                        self.session.state['epoch'] = self.session.state.get('epoch', 0) + 1
                        self.session.state['routing_recoveries'] = self.session.state.get('routing_recoveries', 0) + 1
                        self.session.save('routing_recovery', {'model':payload['model'],'stage':state.value})
                        payload['session_id'] = self.session.cache_identity(state.value) + ':routing-recovery'
                    else:
                        payload['session_id'] = f'atoms:{self.cache_scope or "recovery"}:{uuid.uuid4().hex}'
                    if os.getenv('WORKER_TOKEN'):
                        clean = {k:v for k,v in payload.items() if not k.startswith('_billing_')}
                        payload['_billing_request'] = self.session.request_id(clean, state.value, active_job.get()) if self.session else str(uuid.uuid4())
                    if getattr(self, 'on_recovery', None):
                        self.on_recovery()
                    if attempt == 2:
                        # Do not create an unexecuted "recovered" checkpoint.
                        data = await self._stream(client, payload)
                        break
                    continue
                if isinstance(exc, httpx.HTTPStatusError) and attempt == 2:
                    raise ModelTemporaryError('模型网关暂时不可用，保留检查点并自动恢复。') from exc
                if isinstance(exc, RuntimeError) or '请求失败 (4' in text:
                    pending_checkpoint(clear=True)
                    raise
                await asyncio.sleep(2 ** attempt)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt == 2:
                    raise ModelTemporaryError('模型连接暂时失败，保留检查点并自动恢复。',
                                              pending=not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))) from exc
                await asyncio.sleep(2 ** attempt)
        pending_checkpoint(clear=True)
        if self.on_usage:
            usage = data.get("usage") or {}
            self.on_usage(state.value, {"model": self.model_for(state),
                        'generation_id': data.get('id'), 'cost': usage.get('cost'),
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        'reasoning_tokens': int((usage.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                        'cached_tokens': int((usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0),
                        'cache_write_tokens': int((usage.get('prompt_tokens_details') or {}).get('cache_write_tokens') or 0),
                        "duration_ms": round((time.monotonic() - started) * 1000)})
        choice = data["choices"][0]
        message = choice["message"]
        message['_response_meta'] = {'finish_reason': choice.get('finish_reason'),
                                    'completion_tokens': (data.get('usage') or {}).get('completion_tokens', 0),
                                    'reasoning_tokens': ((data.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens', 0),
                                    'content_chars': len(message.get('content') or ''),
                                    'max_tokens': max_tokens}
        if message.get('reasoning_details'):
            message.pop('reasoning', None)
            message.pop('reasoning_content', None)
        if choice.get("finish_reason") == "length" and not message.get("tool_calls"):
            message["content"] = (message.get("content") or "") + "\n[输出达到长度限制；当前响应不能当作完整交付，请继续。]"
        return message


class ModelContextOverflow(RuntimeError):
    """Recoverable only by reducing the actual request, never by retrying it intact."""


def context_overflow(body: str) -> bool:
    value = body.lower()
    return any(term in value for term in ('context_length_exceeded', 'context length exceeded',
               'maximum context length', 'too many tokens', 'input exceeds the maximum',
               'input token count exceeds', 'request_too_large', 'context window exceeded'))


def _terms(text: str) -> set[str]:
    english = {item.lower() for item in re.findall(r"[A-Za-z][A-Za-z0-9_./-]{2,}", text)}
    chinese = set()
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        chinese.update(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return english | chinese


def retrieve_context(root: Path, paths: list[str], request: str, max_files: int = 12,
                     max_chars: int = 48000) -> str:
    terms = _terms(request)
    ranked = []
    for name in paths[:3000]:
        path = root / name
        try:
            with path.open(encoding='utf-8') as stream:
                content = stream.read(12000)
        except (OSError, UnicodeError):
            continue
        if "\x00" in content:
            continue
        path_text = name.lower()
        content_text = content[:12000].lower()
        score = sum(12 for term in terms if term in path_text)
        score += sum(min(content_text.count(term), 3) for term in terms)
        if name in ("README.md", "package.json", "pyproject.toml", "go.mod", ".atoms-workspace.json"):
            score += 3
        if score:
            ranked.append((score, name, content))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    sections = []
    budget = max_chars
    for _, name, content in ranked[:max_files]:
        excerpt = content[:min(3200, budget)]
        sections.append(f"### {name}\n{excerpt}")
        budget -= len(excerpt)
        if budget <= 0:
            break
    return "\n\n".join(sections) or "未检索到相关文件；请使用文件搜索工具继续探索。"


def symbol_search(root: Path, paths: list[str], query: str) -> str:
    if not query or len(query) > 200:
        raise ValueError("符号搜索词长度需为 1–200 个字符")
    matches = []
    pattern = re.compile(r"^\s*(?:(?:export|async|default|public|private|static)\s+)*"
                         r"(?:function|class|interface|type|const|let|def|func|fn|struct|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)")
    for name in paths[:3000]:
        if not name.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs")):
            continue
        try:
            lines = (root / name).read_text().splitlines()
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(lines, 1):
            found = pattern.match(line)
            if found and query.lower() in found.group(1).lower():
                matches.append(f"{name}:{number}: {line.strip()[:240]}")
                if len(matches) >= 80:
                    return "\n".join(matches)
    return "\n".join(matches) or "未找到匹配符号"


def apply_unified_patch(root: Path, name: str, patch: str, read_file: Callable,
                        write_file: Callable) -> str:
    """Apply exact contextual hunks; stale line numbers never justify fuzzy edits."""
    original = read_file(root, name)
    source = original.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    result, position, index, hunks = [], 0, 0, 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(("--- ", "+++ ")):
            index += 1
            continue
        header = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        contextual = line.rstrip('\r\n') == '@@'
        if not header and not contextual:
            raise ValueError("补丁需要 unified diff hunk 或独立 @@；小范围修改也可使用 replace_in_file")
        index += 1
        old, new = [], []
        while index < len(lines) and not lines[index].startswith('@@'):
            change = lines[index]
            if not change or change[0] not in (' ', '-', '+'):
                raise ValueError("补丁修改行必须以空格、- 或 + 开头；尚未写入文件")
            if change[0] in (' ', '-'):
                old.append(change[1:])
            if change[0] in (' ', '+'):
                new.append(change[1:])
            index += 1
        if header:
            if len(old) != int(header.group(2) or '1') or len(new) != int(header.group(4) or '1'):
                raise ValueError("补丁 hunk 行数不匹配；请重生成正确计数或使用带唯一上下文的 @@，尚未写入")
            start = int(header.group(1)) - (1 if old else 0)
        else:
            start = -1
        if old:
            # Exact line anchor first. If stale, relocation requires one unique,
            # byte-identical sequence; never guess or match whitespace fuzzily.
            if start < position or source[start:start + len(old)] != old:
                matches = [i for i in range(position, len(source) - len(old) + 1)
                           if source[i:i + len(old)] == old]
                if len(matches) != 1:
                    raise ValueError(f"补丁旧内容在 {name} 精确匹配 {len(matches)} 次，需唯一上下文；未写入。读取相关范围后重新生成，或用 replace_in_file")
                start = matches[0]
        elif not header or start < position or start > len(source):
            raise ValueError("纯插入补丁需要有效行号或增加唯一上下文；尚未写入")
        result.extend(source[position:start])
        result.extend(new)
        position = start + len(old)
        hunks += 1
    if not hunks:
        raise ValueError("补丁没有修改")
    result.extend(source[position:])
    updated = ''.join(result)
    if updated == original:
        raise ValueError("补丁未改变文件")
    return write_file(root, name, updated)


def changed_diff(before: dict, after: dict, max_chars: int = 18000) -> str:
    from agent_session import inventory
    old_hashes, new_hashes = inventory(before), inventory(after)
    changed_names = [name for name in set(before) | set(after) if old_hashes.get(name) != new_hashes.get(name) and not name.startswith(".atoms/")]
    source_names = [name for name in changed_names if not name.endswith(
        ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "tsconfig.tsbuildinfo"))]
    source_names.sort(key=lambda name: (
        not name.startswith(("src/", "app/", "pages/", "lib/")),
        name.endswith((".css", ".scss")),
        name))
    sections = ["Changed files: " + ", ".join(sorted(changed_names))]
    remaining = max_chars - len(sections[0])
    for name in source_names:
        if before.get(name) == after.get(name):
            continue
        old = before.get(name, "")
        new = after.get(name, "")
        if not isinstance(old, str) or not isinstance(new, str):
            sections.append(f"{name}: large or binary file changed; use scoped source reads for text")
            continue
        lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                          fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""))
        excerpt = "\n".join(lines)[:min(6000, remaining)]
        sections.append(excerpt)
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    return "\n\n".join(sections)[:max_chars]


def review_sources(before: dict, after: dict, max_chars: int = 50000, *, full: bool = False) -> str:
    changed = list(after) if full else [name for name in set(before) | set(after) if before.get(name) != after.get(name)]
    names = sorted((name for name in changed if not name.startswith(".atoms/")
                    and name.endswith((".tsx", ".ts", ".jsx", ".js", ".py", ".go", ".rs", ".html"))),
                   key=lambda name: (not name.endswith(("App.tsx", "App.jsx", "index.tsx", "index.jsx")), name))
    if not names:
        names = sorted(name for name in after if not name.startswith('.atoms/')
                       and name.endswith(('.tsx', '.ts', '.jsx', '.js', '.py', '.go', '.rs', '.html')))
    sections = []
    remaining = max_chars
    for name in names:
        content = after.get(name)
        if not isinstance(content, str):
            continue
        if len(content) > remaining:
            sections.append(f"{name}: 文件长度 {len(content)} 字符，当前复核上下文不足；不能因此判定未实现")
            continue
        sections.append(f"### {name}（完整当前文件）\n{content}")
        remaining -= len(content)
        if remaining < 2000:
            break
    return "\n\n".join(sections) or "没有可直接显示的源码文件；请结合差异核查。"


def configured_tests(root: Path, planned_commands: list[str] | None = None) -> list[str]:
    config = root / ".atoms-workspace.json"
    if config.exists():
        from execution_contract import workspace_commands
        settings = workspace_commands(root)
        if not isinstance(settings, dict):
            raise ValueError('工作区配置必须为 JSON 对象')
        command = settings.get("test")
        if command:
            commands = [command] if isinstance(command, str) else command
            if not isinstance(commands, list) or not all(isinstance(item, str) and item.strip() for item in commands):
                raise ValueError("工作区 test 命令必须是字符串或字符串列表")
            from execution_contract import validate_command
            for item in commands: validate_command(item)
            return commands
    if planned_commands:
        # A validated plan declares the actual runner. A tests/ directory does
        # not imply pytest, and must never override a unittest/CLI command.
        if not isinstance(planned_commands, list) or not all(isinstance(item, str) and item.strip() for item in planned_commands):
            raise ValueError('计划 test 命令必须为非空字符串列表')
        from execution_contract import validate_command
        for item in planned_commands: validate_command(item)
        return planned_commands
    package = root / "package.json"
    if package.exists():
        scripts = json.loads(package.read_text()).get("scripts", {})
        if scripts.get("test"):
            return ["npm run test"]
    tests = sorted(root.glob('test_*.py')) + sorted((root / 'tests').glob('test_*.py'))
    if tests:
        content = '\n'.join(path.read_text(errors='replace')[:8000] for path in tests[:20])
        if not (root/'pytest.ini').exists() and re.search(r'\b(?:import unittest|from unittest import)\b', content) and not re.search(r'\b(?:import pytest|from pytest import)\b', content):
            return ['python -m unittest discover -s tests' if (root/'tests').is_dir() else 'python -m unittest discover']
        return ['python -m pytest -q']
    return []


def has_build_command(root: Path) -> bool:
    config = root / ".atoms-workspace.json"
    if config.exists():
        from execution_contract import workspace_commands
        settings = workspace_commands(root)
        if not isinstance(settings, dict):
            raise ValueError('工作区配置必须为 JSON 对象')
        if settings.get('build'):
            return True
    package = root / "package.json"
    return bool(package.exists() and json.loads(package.read_text()).get("scripts", {}).get("build"))
