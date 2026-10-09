"""Shared response contracts. Keep domain request/response models near routes."""
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    database: str
