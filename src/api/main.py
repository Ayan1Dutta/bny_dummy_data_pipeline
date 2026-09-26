from __future__ import annotations

import logging
import os
import uuid
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.api import repository, schemas
from src.api.security import rate_limit, verify_api_key
from src.config import settings, utcnow

VERSION = os.getenv("APP_VERSION", "1.0.0")

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

app = FastAPI(
    title="Merchant Settlement Intelligence API",
    version=VERSION,
    description="Settlement performance, gaps and merchant exceptions for Payments Operations.",
    openapi_tags=[
        {"name": "analytics", "description": "Settlement KPIs and exception reporting"},
        {"name": "ops", "description": "Health, readiness and data quality"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["X-API-Key", "Content-Type"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    start = utcnow()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    log.info(
        "request path=%s status=%s duration_ms=%.1f request_id=%s",
        request.url.path,
        response.status_code,
        (utcnow() - start).total_seconds() * 1000,
        request_id,
    )
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error path=%s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "An unexpected error occurred"},
    )


def parse_window(start_date: str, end_date: str) -> tuple[date, date]:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="start_date and end_date must be valid ISO dates (YYYY-MM-DD)") from exc
    if start > end:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    if (end - start).days > settings.max_date_range_days:
        raise HTTPException(status_code=400, detail=f"date range cannot exceed {settings.max_date_range_days} days")
    return start, end


def validate_merchant(merchant_id: str | None) -> str | None:
    if merchant_id is None:
        return None
    merchant_id = merchant_id.strip()
    if not merchant_id:
        return None
    if not repository.merchant_exists(merchant_id):
        raise HTTPException(status_code=404, detail=f"merchant_id {merchant_id} not found")
    return merchant_id


@app.get("/health", response_model=schemas.HealthStatus, tags=["ops"])
def health() -> schemas.HealthStatus:
    return schemas.HealthStatus(status="ok", env=settings.env, version=VERSION)


@app.get("/ready", response_model=schemas.ReadinessStatus, tags=["ops"])
def ready() -> schemas.ReadinessStatus:
    try:
        info = repository.readiness()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="warehouse not reachable") from exc
    return schemas.ReadinessStatus(
        status="ready" if info["gold_rows"] > 0 else "degraded",
        warehouse_reachable=True,
        gold_rows=info["gold_rows"],
        last_batch_id=info["last_batch_id"],
        last_run_status=info["last_run_status"],
    )


@app.get(
    "/api/v1/settlement-summary",
    response_model=schemas.SettlementSummary,
    tags=["analytics"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def settlement_summary(
    start_date: str = Query(..., description="ISO date, inclusive"),
    end_date: str = Query(..., description="ISO date, inclusive"),
    merchant_id: str | None = Query(None, max_length=32, pattern=r"^[A-Za-z0-9_-]{1,32}$"),
) -> schemas.SettlementSummary:
    start, end = parse_window(start_date, end_date)
    merchant = validate_merchant(merchant_id)
    data = repository.settlement_summary(start, end, merchant)
    return schemas.SettlementSummary(
        start_date=start, end_date=end, merchant_id=merchant, generated_at=utcnow(), **data
    )


@app.get(
    "/api/v1/merchant-exceptions",
    response_model=list[schemas.MerchantException],
    tags=["analytics"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def merchant_exceptions(
    start_date: str = Query(...),
    end_date: str = Query(...),
    settlement_rate_threshold: float = Query(settings.settlement_rate_threshold, ge=0, le=100),
    sla_rate_threshold: float = Query(settings.sla_rate_threshold, ge=0, le=100),
    limit: int = Query(50, ge=1, le=500),
) -> list[schemas.MerchantException]:
    start, end = parse_window(start_date, end_date)
    rows = repository.merchant_exceptions(start, end, settlement_rate_threshold, sla_rate_threshold, limit)
    return [schemas.MerchantException(**row) for row in rows]


@app.get(
    "/api/v1/daily-trend",
    response_model=list[schemas.DailyTrendPoint],
    tags=["analytics"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def daily_trend(
    start_date: str = Query(...),
    end_date: str = Query(...),
    merchant_id: str | None = Query(None, max_length=32, pattern=r"^[A-Za-z0-9_-]{1,32}$"),
) -> list[schemas.DailyTrendPoint]:
    start, end = parse_window(start_date, end_date)
    merchant = validate_merchant(merchant_id)
    return [schemas.DailyTrendPoint(**row) for row in repository.daily_trend(start, end, merchant)]


@app.get(
    "/api/v1/top-merchant-gaps",
    response_model=list[schemas.MerchantGap],
    tags=["analytics"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def top_merchant_gaps(
    start_date: str = Query(...),
    end_date: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
) -> list[schemas.MerchantGap]:
    start, end = parse_window(start_date, end_date)
    return [schemas.MerchantGap(**row) for row in repository.top_merchant_gaps(start, end, limit)]


@app.get(
    "/api/v1/data-quality",
    response_model=list[schemas.DataQualitySummaryItem],
    tags=["ops"],
    dependencies=[Depends(verify_api_key), Depends(rate_limit)],
)
def data_quality(limit: int = Query(25, ge=1, le=200)) -> list[schemas.DataQualitySummaryItem]:
    from src.quality.rules import rule

    items = []
    for row in repository.data_quality_summary(limit):
        try:
            reason = rule(row["rule_id"]).description
        except KeyError:
            reason = "unknown rule"
        items.append(schemas.DataQualitySummaryItem(**row, reason=reason))
    return items
