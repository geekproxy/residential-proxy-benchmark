"""
Loads and validates the YAML config files into Pydantic models.

Keeps the runner code clean: parsing/validation lives here, runner just
consumes typed objects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

Difficulty = Literal["trivial", "easy", "medium", "hard", "extreme"]
Category = Literal["baseline", "ecommerce", "search", "cdn", "steam", "social"]


class TargetDefaults(BaseModel):
    timeout_seconds: int = 30
    retries: int = 0
    requests_per_target: int = 100
    user_agent: str


class Target(BaseModel):
    id: str
    name: str
    category: Category
    url: str
    method: str = "GET"
    success_status: list[int] = Field(default_factory=lambda: [200])
    success_body_contains: Optional[str] = None
    block_indicators: list[str] = Field(default_factory=list)
    difficulty: Difficulty
    purpose: str
    requires_browser: bool = False


class TargetSuite(BaseModel):
    version: int
    defaults: TargetDefaults
    baselines: list[Target] = Field(default_factory=list)
    ecommerce: list[Target] = Field(default_factory=list)
    search: list[Target] = Field(default_factory=list)
    cloudflare_akamai: list[Target] = Field(default_factory=list)
    steam: list[Target] = Field(default_factory=list)
    social_browser: list[Target] = Field(default_factory=list)

    def all_targets(self, include_browser: bool = True) -> list[Target]:
        groups = [
            self.baselines,
            self.ecommerce,
            self.search,
            self.cloudflare_akamai,
            self.steam,
            self.social_browser,
        ]
        flat = [t for g in groups for t in g]
        if not include_browser:
            flat = [t for t in flat if not t.requires_browser]
        return flat

    def by_category(self, category: str) -> list[Target]:
        return [t for t in self.all_targets() if t.category == category]

    def by_id(self, target_id: str) -> Optional[Target]:
        for t in self.all_targets():
            if t.id == target_id:
                return t
        return None


class CountrySuite(BaseModel):
    version: int
    description: str = ""
    countries: list[str]


def load_targets(path: str | Path = "config/targets.yaml") -> TargetSuite:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return TargetSuite.model_validate(raw)


def load_countries(path: str | Path = "config/countries.yaml") -> CountrySuite:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return CountrySuite.model_validate(raw)
