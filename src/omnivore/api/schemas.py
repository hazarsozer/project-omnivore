from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str


class APIResponse(BaseModel, Generic[T]):  # noqa: UP046
    success: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: dict | None = None
