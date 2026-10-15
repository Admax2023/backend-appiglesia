from __future__ import annotations

import math
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, StringConstraints

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class InModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[Any], total: int, page: int, page_size: int) -> "Page[T]":
        return cls(
            items=items, total=total, page=page, page_size=page_size, pages=math.ceil(total / page_size) if total else 0
        )


class ErrorOut(BaseModel):
    detail: str
    code: str


Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]
OptionalNote = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None
