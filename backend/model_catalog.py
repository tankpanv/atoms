"""The model_list file is the canonical curated catalogue for the Agent."""

from __future__ import annotations

import os
import math
from pathlib import Path

import httpx


DEFAULT_MODEL = "openai/gpt-6-luna"
MODEL_LIST_PATH = Path(os.getenv("MODEL_LIST_PATH", Path(__file__).resolve().parent.parent / "model_list"))
PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Claude",
    "deepseek": "DeepSeek",
    "z-ai": "智谱 GLM",
    "moonshotai": "Kimi",
    "x-ai": "xAI",
}


def catalog():
    """Parse provider/model/context/prices from the shared model_list file."""
    lines = MODEL_LIST_PATH.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"模型列表为空：{MODEL_LIST_PATH}")
    models = []
    seen = set()
    for line_number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        columns = line.split()
        if len(columns) != 5:
            raise ValueError(f"model_list 第 {line_number} 行应有 5 列")
        provider, name, context, input_price, output_price = columns
        if int(context) <= 0 or any(not math.isfinite(float(p)) or float(p) < 0 for p in (input_price, output_price)):
            raise ValueError(f'model_list 第 {line_number} 行的上下文或价格无效')
        model_id = f"{provider}/{name}"
        if model_id in seen:
            raise ValueError(f"model_list 存在重复模型：{model_id}")
        seen.add(model_id)
        models.append({
            "provider": PROVIDER_LABELS.get(provider, provider),
            "name": name,
            "id": model_id,
            "context": int(context),
            "input_price": float(input_price),
            "output_price": float(output_price),
        })
    return models


def catalog_ids():
    return {model["id"] for model in catalog()}


def canonical_model_id(model_id: str) -> str:
    """Normalize a provider wire name to the canonical model_list ID."""
    value = str(model_id or '').strip()
    entries = catalog()
    if any(item['id'] == value for item in entries):
        return value
    matches = [item['id'] for item in entries if item['name'] == value]
    return matches[0] if len(matches) == 1 else value


async def live_catalog():
    """Return model_list entries confirmed in OpenRouter's current catalogue."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
    by_id = {item["id"]: item for item in response.json()["data"]}
    models = []
    for model in catalog():
        live = by_id.get(model["id"])
        if not live:
            continue
        modalities = set((live.get("architecture") or {}).get("input_modalities") or [])
        models.append({**model, "available": True, "image_input": "image" in modalities})
    return models
