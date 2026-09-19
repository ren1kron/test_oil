from __future__ import annotations

import math
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def day_index(value: str, first_year: int = 2035) -> int:
    d = date.fromisoformat(value)
    if d.month == 2 and d.day == 29:
        raise ValueError("Модельный год содержит 365 дней; 29 февраля не используется")
    return (d.year - first_year) * 365 + sum(MONTH_LENGTHS[: d.month - 1]) + d.day - 1


def day_label(day: int, first_year: int = 2035) -> str:
    year, remainder = divmod(day, 365)
    month = 1
    for length in MONTH_LENGTHS:
        if remainder < length:
            break
        remainder -= length
        month += 1
    return f"{first_year + year:04d}-{month:02d}-{remainder + 1:02d}"


def lead_days(value: float, unit: str) -> int:
    return math.ceil(value * {"day": 1, "week": 7, "month": 365 / 12, "year": 365}[unit])


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Order(Record):
    source_id: str
    order_date: str
    delivery_date: str
    volume_t: float = Field(ge=0)
    startup: bool = False
    contingency: bool = False

    @field_validator("order_date", "delivery_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        day_index(value)
        return value


class Reservation(Record):
    source_id: str
    year: int
    annual_capacity_t: float = Field(ge=0)
    start_day: int = Field(default=0, ge=0, le=364)
    end_day: int = Field(default=365, ge=1, le=365)

    @model_validator(mode="after")
    def ordered_period(self):
        if self.end_day <= self.start_day:
            raise ValueError("Конец договора должен быть позже начала")
        return self


class Investment(Record):
    investment_id: Literal["EARTH_NEW", "LUNAR_ISRU", "ZBO"]
    decision_date: str
    option_date: str | None = None

    @field_validator("decision_date", "option_date")
    @classmethod
    def valid_date(cls, value: str | None) -> str | None:
        if value is not None:
            day_index(value)
        return value


class Assumptions(Record):
    lead_time_policy: Literal["upper", "lower"] = "upper"
    opening_stock_funding: str = "Стартовая партия оплачивается и учитывается в договорах первого года"
    calendar: Literal["365_day"] = "365_day"
    reserve_policy: Literal["physical"] = "physical"
    emergency_base_definition: str = "Любая заранее запланированная закупка E; активации отдельных риск-сценариев исключены"
    notes: list[str] = Field(default_factory=list)


class Decisions(Record):
    supply_orders: list[Order] = Field(default_factory=list)
    capacity_reservations: list[Reservation] = Field(default_factory=list)
    investments: list[Investment] = Field(default_factory=list)
    inventory_policy: dict[str, float] = Field(default_factory=lambda: {"target_days": 45.0})

    @field_validator("inventory_policy")
    @classmethod
    def valid_inventory(cls, value):
        if any(not math.isfinite(x) or x < 0 for x in value.values()):
            raise ValueError("Цели запаса должны быть конечными и неотрицательными")
        return value


class Plan(Record):
    schema_version: Literal[1] = 1
    plan_id: str = Field(min_length=1, pattern=r"^[\w.-]+$")
    scenario_id: str = "BASE"
    decisions: Decisions = Field(default_factory=Decisions)
    assumptions: Assumptions = Field(default_factory=Assumptions)
    description: str = ""


class Risk(Record):
    risk_id: str = Field(pattern=r"^TEAM_[\w-]+$")
    event: str
    source_id: str
    start_year: int
    end_year: int
    delivery_share: float = Field(default=1, ge=0, le=1)
    price_multiplier: float = Field(default=1, gt=0)
    delay_days: int = Field(default=0, ge=0)
    probability_basis_or_range: str = "TEAM_ASSUMPTION: сценарий, вероятность не задана"
    cause: str = "Сценарное допущение команды"
    owner: str = "Оператор узла"
    mitigation: str = "Уменьшить длительность задержки / недопоставки"
    dependencies: str = "Не комбинируется с обязательным стрессом"
    residual_delivery_share: float = Field(default=1, ge=0, le=1)
    residual_delay_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_period(self):
        if self.end_year < self.start_year:
            raise ValueError("Некорректный период риска")
        return self
