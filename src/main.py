import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src import scheduler
from src.common.bitnob_client import BitnobError
from src.config import settings
from src.payments.paystack import PaystackError
from src.admin.routes import router as admin_router
from src.auth.routes import router as auth_router
from src.cards.routes import router as cards_router
from src.crossborder.routes import router as crossborder_router
from src.payments.refresh_routes import router as payment_refresh_router
from src.payments.routes import router as payments_router
from src.splitbill.routes import router as splitbill_router
from src.vaults.routes import router as vaults_router
from src.wallet.routes import router as wallet_router


# Without this nothing below WARNING from the app's own modules was ever shown -
# e.g. the Bitnob-balance fallback and payout outcomes. httpx logs every request
# URL at INFO (including phone numbers in Bitnob query strings), so it stays quiet.
logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for _noisy in ("httpx", "httpcore", "apscheduler"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(
    title="GlobePay API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/v1/docs",
    openapi_url="/v1/openapi.json",  # Explicitly align openapi JSON path with /v1
)

@app.exception_handler(PaystackError)
async def _paystack_error(_request: Request, exc: PaystackError) -> JSONResponse:
    """Safety net: a provider failure nobody handled locally is a 502 with a
    readable reason, not a bare "Internal Server Error" (which the app can't
    show and which also drops CORS headers, so the browser sees a network error)."""
    return JSONResponse(status_code=502, content={"detail": f"Payment provider error: {exc}"})


@app.exception_handler(BitnobError)
async def _bitnob_error(_request: Request, exc: BitnobError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": f"Card/transfer provider error: {exc.user_message()}"})


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(vaults_router)
app.include_router(wallet_router)
app.include_router(splitbill_router)
app.include_router(crossborder_router)
app.include_router(cards_router)
app.include_router(payments_router)
app.include_router(payment_refresh_router)
app.include_router(admin_router)


@app.get("/")
async def root():
    return {"status": "ok", "message": "GlobePay API is running"}


@app.get("/health")
async def health():
    return {"status": "ok"}