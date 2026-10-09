from contextvars import ContextVar

active_job: ContextVar[str] = ContextVar('billing_job', default='')
