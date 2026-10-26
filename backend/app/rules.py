"""Typed, validated clinic rules loaded from YAML.

The dialogue brain never hard-codes hours, services or policy: it reads them from here.
Invalid configuration fails at start-up, not in the middle of a call.
"""

from __future__ import annotations

from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, model_validator

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
Requires = Literal["any", "new", "established"]


class ClinicInfo(BaseModel):
    name: str
    name_ur: str | None = None
    emergency_en: str = "911"
    emergency_ur: str = "1122"
    timezone: str
    phone: str
    address: str
    parking: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Policy(BaseModel):
    cancel_notice_hours: int = 24
    late_arrival_grace_minutes: int = 15
    arrive_early_new_minutes: int = 30
    arrive_early_established_minutes: int = 15
    min_lead_minutes: int = 120
    max_advance_days: int = 90
    slot_step_minutes: int = Field(15, ge=5, le=60)
    max_verify_attempts: int = 3
    max_options: int = Field(3, ge=1, le=5)
    search_horizon_days: int = 21
    new_patient_service: str
    default_service_for_new_patients: str


class Service(BaseModel):
    key: str
    name: str
    duration_minutes: int = Field(gt=0)
    requires: Requires = "any"
    keywords: list[str] = []
    name_ur: str | None = None


class Provider(BaseModel):
    key: str
    name: str
    title: str
    specialty: str
    color: str
    name_ur: str | None = None
    title_ur: str | None = None
    services: list[str]
    schedule: dict[str, list[tuple[str, str]]]

    def windows(self, weekday: int) -> list[tuple[time, time]]:
        raw = self.schedule.get(WEEKDAYS[weekday], [])
        return [(time.fromisoformat(a), time.fromisoformat(b)) for a, b in raw]

    @property
    def spoken_name(self) -> str:
        return f"{self.title} {self.name.split()[-1]}"

    @property
    def spoken_name_ur(self) -> str:
        if not self.name_ur:
            return self.spoken_name
        return f"{self.title_ur or self.title} {self.name_ur.split()[-1]}"

    @property
    def full_name_ur(self) -> str:
        return f"{self.title_ur or self.title} {self.name_ur}" if self.name_ur else f"{self.title} {self.name}"


class FaqEntry(BaseModel):
    id: str
    question: str
    keywords: list[str]
    answer: str
    question_ur: str | None = None
    answer_ur: str | None = None


class ClinicRules(BaseModel):
    clinic: ClinicInfo
    policy: Policy
    holidays: list[date] = []
    services: list[Service]
    providers: list[Provider]
    emergency_keywords: list[str]
    faq: list[FaqEntry]

    @model_validator(mode="after")
    def _cross_check(self) -> ClinicRules:
        keys = {s.key for s in self.services}
        if len(keys) != len(self.services):
            raise ValueError("duplicate service keys")
        if len({p.key for p in self.providers}) != len(self.providers):
            raise ValueError("duplicate provider keys")
        for p in self.providers:
            unknown = set(p.services) - keys
            if unknown:
                raise ValueError(f"provider {p.key} offers unknown services {sorted(unknown)}")
            for day, windows in p.schedule.items():
                if day not in WEEKDAYS:
                    raise ValueError(f"provider {p.key}: bad weekday '{day}'")
                for a, b in windows:
                    if time.fromisoformat(a) >= time.fromisoformat(b):
                        raise ValueError(f"provider {p.key}: window {a}-{b} is not increasing")
        for k in (self.policy.new_patient_service, self.policy.default_service_for_new_patients):
            if k not in keys:
                raise ValueError(f"policy references unknown service '{k}'")
        for s in self.services:
            if not any(s.key in p.services for p in self.providers):
                raise ValueError(f"service {s.key} has no provider")
        return self

    # -- lookups -----------------------------------------------------------------------
    def service(self, key: str) -> Service:
        return next(s for s in self.services if s.key == key)

    def provider(self, key: str) -> Provider:
        return next(p for p in self.providers if p.key == key)

    def providers_for(self, service_key: str) -> list[Provider]:
        return [p for p in self.providers if service_key in p.services]

    def is_holiday(self, d: date) -> bool:
        return d in self.holidays

    def clinic_open_days(self) -> set[int]:
        return {i for i, day in enumerate(WEEKDAYS) for p in self.providers if p.schedule.get(day)}


@lru_cache
def load_rules(path: str | Path) -> ClinicRules:
    with open(path, encoding="utf-8") as fh:
        return ClinicRules.model_validate(yaml.safe_load(fh))
