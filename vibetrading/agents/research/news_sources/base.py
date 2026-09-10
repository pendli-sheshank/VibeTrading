from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel

from vibetrading.core.models import Stock


class NewsItem(BaseModel):
    title: str
    summary: str = ""
    source: str
    url: str = ""
    published_at: datetime


class NewsSource(ABC):
    @abstractmethod
    async def fetch(self, stock: Stock) -> list[NewsItem]:
        ...
