from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import scheduler
from src.admin.routes import router as admin_router
from src.auth.routes import router as auth_router
from src.cards.routes import router as cards_router
from src.crossborder.routes import router as crossborder_router
from src.payments.refresh_routes import router as payment_refresh_router
from src.payments.routes import router as payments_router
from src.splitbill.routes import router as splitbill_router
from src.vaults.routes import router as vaults_router
from src.wallet.routes import router as wallet_router


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