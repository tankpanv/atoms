"""Versioned, trusted expert skills and durable user/project selections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from auth import connection, current_user

ROOT = Path(__file__).parent / 'expert_skills'
router = APIRouter(prefix='/api/experts', tags=['experts'])


def catalogue():
    return json.loads((ROOT / 'catalog.json').read_text())


def expert_by_id(expert_id: str):
    expert = next((item for item in catalogue() if item['id'] == expert_id), None)
    if not expert:
        raise HTTPException(422, f'专家不存在或已不可用：{expert_id}')
    return expert


def validate_experts(ids: list[str]):
    if len(ids) > 3:
        raise HTTPException(422, '一次最多选择 3 位专家')
    result = list(dict.fromkeys(ids))
    for expert_id in result:
        expert_by_id(expert_id)
    return result


def snapshot_experts(ids: list[str]):
    snapshots = []
    for expert_id in validate_experts(ids):
        expert = expert_by_id(expert_id)
        skill = (ROOT / expert['skill'] / 'SKILL.md').read_text()
        snapshots.append({'id': expert_id, 'name': expert['name'], 'skill': expert['skill'],
                          'version': expert['version'], 'sha256': hashlib.sha256(skill.encode()).hexdigest(),
                          'instructions': skill})
    return snapshots


def skill_context(snapshots: list[dict]):
    if not snapshots:
        return ''
    return ('\n\n用户选择了以下专家。将这些 skill 用于当前需求的理解、规划、实现和复核；'
            '遵守用户需求与项目约束，专家不会扩大用户授权范围。\n' +
            '\n\n'.join(f"## {item['name']} · {item['skill']} · v{item['version']}\n{item['instructions']}"
                        for item in snapshots))


def init_experts_db(conn):
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS expert_ids JSONB NOT NULL DEFAULT '[]'::jsonb")
    conn.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS expert_ids JSONB NOT NULL DEFAULT '[]'::jsonb")
    conn.execute("CREATE TABLE IF NOT EXISTS user_experts (user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE, expert_id TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), PRIMARY KEY(user_id,expert_id))")


@router.get('')
def list_experts(user=Depends(current_user)):
    with connection() as conn:
        saved = {row['expert_id'] for row in conn.execute('SELECT expert_id FROM user_experts WHERE user_id=%s', (user['id'],)).fetchall()}
        counts = conn.execute("SELECT expert_id,COUNT(*) AS uses FROM agent_jobs j CROSS JOIN LATERAL jsonb_array_elements_text(j.expert_ids) AS expert_id JOIN projects p ON p.id=j.project_id WHERE p.owner_id=%s GROUP BY expert_id", (user['id'],)).fetchall()
    uses = {row['expert_id']: row['uses'] for row in counts}
    return [{**item, 'saved': item['id'] in saved, 'uses': uses.get(item['id'], 0)} for item in catalogue()]


@router.get('/{expert_id}')
def get_expert(expert_id: str, user=Depends(current_user)):
    return next(item for item in list_experts(user) if item['id'] == expert_by_id(expert_id)['id'])


class SavedExpert(BaseModel):
    saved: bool


@router.put('/{expert_id}/saved')
def save_expert(expert_id: str, data: SavedExpert, user=Depends(current_user)):
    expert_by_id(expert_id)
    with connection() as conn:
        if data.saved:
            conn.execute('INSERT INTO user_experts(user_id,expert_id) VALUES(%s,%s) ON CONFLICT DO NOTHING', (user['id'], expert_id))
        else:
            conn.execute('DELETE FROM user_experts WHERE user_id=%s AND expert_id=%s', (user['id'], expert_id))
    return {'saved': data.saved}


class ExpertSelection(BaseModel):
    expert_ids: list[str] = Field(default_factory=list, max_length=3)


@router.get('/{expert_id}/examples/{example_id}', response_class=HTMLResponse)
def example_preview(expert_id: str, example_id: str):
    expert = expert_by_id(expert_id)
    example = next((item for item in expert['examples'] if item['id'] == example_id), None)
    if not example:
        raise HTTPException(404, '案例不存在')
    # Only registry-owned filenames are read; user-supplied paths never reach disk.
    response = HTMLResponse((ROOT / 'examples' / example['file']).read_text())
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response
