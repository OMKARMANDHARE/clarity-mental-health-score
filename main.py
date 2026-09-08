"""
Mental Health Score Predictor API

"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = Path(os.getenv("MODEL_PATH", BASE_DIR / "model.joblib"))
STATIC_DIR = BASE_DIR / "static"


ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("mental_health_api")

ml_model = {}  


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MODEL_PATH.exists():
        raise RuntimeError(
            f"model.joblib not found at {MODEL_PATH}. "
            "Set MODEL_PATH env var or place the file next to main.py."
        )
    logger.info("Loading model from %s", MODEL_PATH)
    ml_model["pipeline"] = joblib.load(MODEL_PATH)
    logger.info("Model loaded successfully")
    yield
    ml_model.clear()


app = FastAPI(
    title="Mental Health Score API",
    description="Predicts a student's mental health score from social media & lifestyle habits.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------


class MentalHealthInput(BaseModel):
    model_config = {"extra": "forbid"}

    Age: Annotated[int, Field(..., gt=0, lt=100, description="Age in years", examples=[21])]
    Gender: Annotated[Literal["Male", "Female"], Field(..., description="Gender")]
    Country: Annotated[
        Literal[
            "India", "USA", "Canada", "Australia", "UK",
            "Germany", "Turkey", "Mexico", "France", "Spain", "Other",
        ],
        Field(..., description="Country"),
    ]
    Academic_Level: Annotated[
        Literal["High School", "Undergraduate", "Graduate"],
        Field(..., description="Current academic level"),
    ]
    Most_Used_Platform: Annotated[
        Literal[
            "Facebook", "Instagram", "KakaoTalk", "LINE", "LinkedIn",
            "Snapchat", "TikTok", "Twitter", "VKontakte", "WeChat",
            "WhatsApp", "YouTube",
        ],
        Field(..., description="Most used social media platform"),
    ]
    Purpose_Of_Use: Annotated[
        Literal["Education", "Entertainment", "Networking", "News"],
        Field(..., description="Primary purpose of social media use"),
    ]
    Avg_Daily_Usage_Hours: Annotated[
        float, Field(..., ge=0, le=24, description="Average daily social media usage in hours")
    ]
    Study_Hours: Annotated[float, Field(..., ge=0, le=24, description="Average daily study hours")]
    Physical_Activity_Hours: Annotated[
        float, Field(..., ge=0, le=24, description="Average daily physical activity in hours")
    ]
    Sleep_Hours_Per_Night: Annotated[
        float, Field(..., ge=0, le=24, description="Average sleep per night in hours")
    ]
    Stress_Level: Annotated[
        Literal["Low", "Medium", "High", "Very High"], Field(..., description="Self-reported stress level")
    ]


class Insight(BaseModel):
    key: str
    label: str


class MentalHealthOutput(BaseModel):
    score: float
    score_out_of: float = 10.0
    category_key: str
    category_label: str
    summary: str
    insights: list[Insight]


# --------------------------------------------------------------------------
# Scoring interpretation
# --------------------------------------------------------------------------

CATEGORY_BANDS: list[tuple[float, str, str, str]] = [
    (8.0, "thriving", "Thriving", "Your habits are largely working in your favor right now."),
    (6.0, "steady", "Steady", "You're holding a decent balance, with some room to tighten up."),
    (4.0, "wobbly", "Wobbly", "A few habits are pulling your wellbeing down more than they should."),
    (2.0, "low", "Running Low", "Several factors are working against you at once — worth addressing."),
    (0.0, "critical", "Redlining", "Your current routine is taking a real toll. Small changes here matter a lot."),
]


def categorize(score: float) -> tuple[str, str, str]:
    for threshold, key, label, summary in CATEGORY_BANDS:
        if score >= threshold:
            return key, label, summary
    return CATEGORY_BANDS[-1][1], CATEGORY_BANDS[-1][2], CATEGORY_BANDS[-1][3]


def build_insights(data: MentalHealthInput) -> list[Insight]:
    insights: list[Insight] = []

    if data.Sleep_Hours_Per_Night < 6:
        insights.append(Insight(key="sleep", label="Sleep is under 6 hours a night — this is likely the single biggest lever you have."))
    elif data.Sleep_Hours_Per_Night >= 8:
        insights.append(Insight(key="sleep_good", label="Solid sleep — that's protecting you more than it might feel like."))

    if data.Avg_Daily_Usage_Hours > 6:
        insights.append(Insight(key="usage", label="Daily usage is quite high; even trimming an hour tends to help."))
    elif data.Avg_Daily_Usage_Hours <= 2:
        insights.append(Insight(key="usage_good", label="Your screen time is already on the lower side — good sign."))

    if data.Physical_Activity_Hours < 1:
        insights.append(Insight(key="activity", label="Very little movement in your day — even short walks tend to shift mood."))

    if data.Stress_Level in ("High", "Very High"):
        insights.append(Insight(key="stress", label="Reported stress is high, which weighs heavily on the score."))

    if data.Study_Hours > 8 and data.Physical_Activity_Hours < 1:
        insights.append(Insight(key="burnout", label="Heavy study load with little activity is a common burnout combo."))

    if not insights:
        insights.append(Insight(key="balanced", label="Your habits look fairly balanced across the board."))

    return insights[:3]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@app.get("/api/health")
def health_check():
    return {"status": "ok", "model_loaded": "pipeline" in ml_model}


@app.post("/api/predict", response_model=MentalHealthOutput)
def predict(data: MentalHealthInput) -> MentalHealthOutput:
    pipeline = ml_model.get("pipeline")
    if pipeline is None:
        raise HTTPException(status_code=503, detail="Model is not loaded yet. Try again shortly.")

    input_df = pd.DataFrame([data.model_dump()])

    try:
        raw_score = float(pipeline.predict(input_df)[0])
    except Exception:
        logger.exception("Prediction failed for input: %s", data.model_dump())
        raise HTTPException(status_code=500, detail="Couldn't generate a prediction from this input.")

    score = round(max(0.0, min(10.0, raw_score)), 1)
    category_key, category_label, summary = categorize(score)

    return MentalHealthOutput(
        score=score,
        category_key=category_key,
        category_label=category_label,
        summary=summary,
        insights=build_insights(data),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = [
        {"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": "Invalid input", "errors": errors})


# --------------------------------------------------------------------------
# Frontend 
# --------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/")
    def serve_frontend():
        return FileResponse(STATIC_DIR / "index.html")
