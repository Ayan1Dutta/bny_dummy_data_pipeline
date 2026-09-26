from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class SettlementSummary(BaseModel):
    start_date: date
    end_date: date
    merchant_id: str | None = None
    transaction_count: int = Field(ge=0, description="Count of SUCCESS transactions in the window")
    transaction_amount: float = Field(ge=0, description="Value of SUCCESS transactions")
    settled_amount: float = Field(ge=0)
    settlement_rate: float = Field(ge=0, le=100)
    settlement_gap: float
    sla_rate: float = Field(ge=0, le=100)
    unsettled_count: int = Field(ge=0)
    generated_at: datetime


class MerchantException(BaseModel):
    merchant_id: str
    merchant_name: str
    settlement_rate: float = Field(ge=0, le=100)
    sla_rate: float = Field(ge=0, le=100)
    risk_level: str
    settlement_gap: float
    transaction_amount: float
    unsettled_count: int
    breach_reasons: list[str]


class DailyTrendPoint(BaseModel):
    transaction_date: date
    transaction_amount: float
    settled_amount: float
    settlement_gap: float
    settlement_rate: float
    sla_rate: float


class MerchantGap(BaseModel):
    merchant_id: str
    merchant_name: str
    risk_level: str
    settlement_gap: float
    settlement_rate: float


class DataQualitySummaryItem(BaseModel):
    rule_id: str
    source_table: str
    severity: str
    disposition: str
    failed_rows: int
    reason: str


class HealthStatus(BaseModel):
    status: str
    env: str
    version: str


class ReadinessStatus(BaseModel):
    status: str
    warehouse_reachable: bool
    gold_rows: int
    last_batch_id: str | None = None
    last_run_status: str | None = None


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: str | None = None
