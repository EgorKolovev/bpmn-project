import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.budget import DailyBudgetExceededError, BudgetTracker
from app.config import (
    BPMN_XML_CHAR_LIMIT,
    DAILY_SPEND_LIMIT_USD,
    DEFAULT_MODEL,
    MAX_OUTPUT_TOKENS,
    REQUEST_CHAR_LIMIT,
    USAGE_BUDGET_TIMEZONE,
    USAGE_DB_PATH,
    get_input_price_per_million_usd,
    get_output_price_per_million_usd,
)
from app.llm import LLMClient, LLMClientError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

llm_client: LLMClient = None
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global llm_client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is required")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    budget_tracker = BudgetTracker(
        db_path=USAGE_DB_PATH,
        daily_limit_usd=DAILY_SPEND_LIMIT_USD,
        input_price_per_million_usd=get_input_price_per_million_usd(model),
        output_price_per_million_usd=get_output_price_per_million_usd(model),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timezone_name=USAGE_BUDGET_TIMEZONE,
    )
    llm_client = LLMClient(
        api_key=api_key,
        model=model,
        budget_tracker=budget_tracker,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    logger.info(
        "ML Service started with model %s and daily cap $%.2f",
        model,
        DAILY_SPEND_LIMIT_USD,
    )
    yield
    await llm_client.close()
    logger.info("ML Service shut down")


app = FastAPI(title="BPMN ML Service", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def verify_internal_api_key(request: Request, call_next):
    # Skip auth for health check
    if request.url.path == "/health":
        return await call_next(request)

    if INTERNAL_API_KEY:
        provided_key = request.headers.get("X-Internal-Api-Key", "")
        if provided_key != INTERNAL_API_KEY:
            logger.warning(
                "Unauthorized ML service request from %s to %s",
                request.client.host if request.client else "unknown",
                request.url.path,
            )
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )

    return await call_next(request)


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(
        ...,
        min_length=1,
        max_length=REQUEST_CHAR_LIMIT,
        description="Business process description",
    )


class GenerateResponse(BaseModel):
    bpmn_xml: str
    session_name: str


class EditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=REQUEST_CHAR_LIMIT,
        description="Edit instruction",
    )
    bpmn_xml: str = Field(
        ...,
        min_length=1,
        max_length=BPMN_XML_CHAR_LIMIT,
        description="Current BPMN XML",
    )


class EditResponse(BaseModel):
    bpmn_xml: str


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/generate", response_model=GenerateResponse)
async def generate(request: GenerateRequest):
    try:
        result = await llm_client.generate(request.description)
        return GenerateResponse(**result)
    except DailyBudgetExceededError as exc:
        logger.warning("Generation blocked by budget cap: %s", exc)
        raise HTTPException(status_code=429, detail=str(exc))
    except LLMClientError as exc:
        logger.error("Generation failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail="Processing failed.")
    except Exception:
        logger.exception("Generation failed unexpectedly")
        raise HTTPException(status_code=500, detail="Processing failed.")


@app.post("/edit", response_model=EditResponse)
async def edit(request: EditRequest):
    try:
        result = await llm_client.edit(request.prompt, request.bpmn_xml)
        return EditResponse(**result)
    except DailyBudgetExceededError as exc:
        logger.warning("Edit blocked by budget cap: %s", exc)
        raise HTTPException(status_code=429, detail=str(exc))
    except LLMClientError as exc:
        logger.error("Edit failed: %s", exc)
        raise HTTPException(status_code=exc.status_code, detail="Processing failed.")
    except Exception:
        logger.exception("Edit failed unexpectedly")
        raise HTTPException(status_code=500, detail="Processing failed.")
