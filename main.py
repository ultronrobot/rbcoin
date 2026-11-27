"""RB Coin - SINOV (bepul) backend. Bitta fayl: pul hisobi, auth, baza, API, bot webhook."""
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import types
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import httpx
import jwt
from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import (BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint,
                        create_engine, or_, select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

# ===== CORE =====
"""Sof (toza) mantiq: pul hisob-kitobi va Telegram initData tekshiruvi.
Tashqi kutubxonasiz (faqat stdlib) - oson test qilinadi."""

DECIMALS = 8
UNIT = 10 ** DECIMALS          # 1 RBC = 100_000_000 birlik
FEE_PERCENT = 2                # platforma komissiyasi, %
MAX_SUPPLY = 100_000_000 * UNIT


class RBCError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def to_units(amount: str) -> int:
    """'12.5' -> 1_250_000_000. Float ishlatilmaydi."""
    try:
        s = str(amount).strip()
        if s.startswith("-") or s == "":
            raise ValueError
        whole, _, frac = s.partition(".")
        if len(frac) > DECIMALS or not (whole or frac):
            raise ValueError
        value = int(whole or "0") * UNIT + int(frac.ljust(DECIMALS, "0") or "0")
    except ValueError:
        raise RBCError("bad_amount", "Miqdor noto'g'ri (maks. 8 kasr xona)")
    if value <= 0:
        raise RBCError("bad_amount", "Miqdor musbat bo'lishi kerak")
    return value


def from_units(value: int) -> str:
    whole, frac = divmod(value, UNIT)
    return f"{whole}.{frac:08d}"


def calc_fee(amount: int) -> int:
    """2% komissiya, pastga yaxlitlanadi."""
    return amount * FEE_PERCENT // 100


def transfer_amounts(amount: int, mode: str):
    """Qaytaradi: (yuboruvchidan yechiladi, qabul qiluvchi oladi, zaxiraga).
    extra   : 100 kiritilsa -> 102 yechiladi, 100 yetadi, 2 zaxira
    included: 100 kiritilsa -> 100 yechiladi, 98 yetadi, 2 zaxira"""
    fee = calc_fee(amount)
    if mode == "extra":
        return amount + fee, amount, fee
    if mode == "included":
        return amount, amount - fee, fee
    raise RBCError("bad_fee_mode", "fee_mode 'extra' yoki 'included' bo'lishi kerak")


def verify_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """Telegram Mini App initData imzosini tekshiradi va user ma'lumotini qaytaradi."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise RBCError("bad_init_data", "initData imzosi yo'q", 401)
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise RBCError("bad_init_data", "initData imzosi noto'g'ri", 401)
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > max_age:
        raise RBCError("expired_init_data", "initData muddati o'tgan", 401)
    try:
        user = json.loads(pairs["user"])
        user["id"] = int(user["id"])
    except (KeyError, ValueError, TypeError):
        raise RBCError("bad_init_data", "user ma'lumoti yo'q", 401)
    return user

# ===== CONFIG =====
def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./rbcoin_dev.db")
    # Render "postgres://" beradi; SQLAlchemy + psycopg3 uchun o'zgartiramiz
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class config:
    DATABASE_URL = _db_url()
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    JWT_SECRET = os.getenv("JWT_SECRET", "")
    JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "3600"))
    PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")        # backend manzili
    WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")        # frontend manzili
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
    CORS_ORIGINS = [o for o in os.getenv("CORS_ORIGINS", WEBAPP_URL).split(",") if o]
    # FAQAT SINOV: "1" bo'lsa /dev/faucet orqali TEST balans olinadi
    TEST_FAUCET = os.getenv("TEST_FAUCET", "0") == "1"
    TEST_FAUCET_AMOUNT = os.getenv("TEST_FAUCET_AMOUNT", "1000")

# ===== DB =====

_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, pool_pre_ping=True, connect_args=_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass

# ===== MODELS =====
def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="uz")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Wallet(Base):
    __tablename__ = "wallets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    wallet_id: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)   # 1e-8 birliklarda
    status: Mapped[str] = mapped_column(String(16), default="active")


class ProjectReserve(Base):
    __tablename__ = "project_reserve"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("sender_wallet_id", "reference"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(16))        # transfer | test_faucet
    sender_wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), nullable=True, index=True)
    receiver_wallet_id: Mapped[int | None] = mapped_column(ForeignKey("wallets.id"), nullable=True, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)       # qabul qiluvchi oladigan summa
    fee: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sender_debit: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="completed")
    reference: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

# ===== SERVICES =====
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_wallet_id() -> str:
    return "RB" + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def get_or_create_user(db: Session, tg: dict) -> tuple[User, Wallet]:
    user = db.scalar(select(User).where(User.telegram_id == tg["id"]))
    if not user:
        user = User(
            telegram_id=tg["id"],
            username=tg.get("username"),
            first_name=tg.get("first_name"),
            language=(tg.get("language_code") or "uz")[:2],
        )
        db.add(user)
        db.flush()
        db.add(Wallet(user_id=user.id, wallet_id=_new_wallet_id()))
        db.flush()
    wallet = db.scalar(select(Wallet).where(Wallet.user_id == user.id))
    return user, wallet


def get_reserve(db: Session) -> ProjectReserve:
    r = db.scalar(select(ProjectReserve).with_for_update())
    if not r:
        r = ProjectReserve(balance=0)
        db.add(r)
        db.flush()
    return r


def find_wallet(db: Session, recipient: str) -> Wallet | None:
    """Wallet ID ('RB...') yoki Telegram ID bo'yicha qidiradi."""
    recipient = recipient.strip()
    if recipient.upper().startswith("RB"):
        return db.scalar(select(Wallet).where(Wallet.wallet_id == recipient.upper()))
    if recipient.lstrip("@").isdigit():
        u = db.scalar(select(User).where(User.telegram_id == int(recipient.lstrip("@"))))
        if u:
            return db.scalar(select(Wallet).where(Wallet.user_id == u.id))
    return None


def do_transfer(db: Session, sender: Wallet, recipient: str, amount_str: str,
                fee_mode: str, reference: str) -> Transaction:
    """Atomik ichki transfer. Chaqiruvchi commit/rollback qiladi."""
    if not reference or len(reference) > 64:
        raise RBCError("bad_reference", "reference (idempotency kaliti) majburiy, maks. 64 belgi")
    # Idempotency: shu reference oldin ishlatilgan bo'lsa, o'sha natijani qaytaramiz
    prev = db.scalar(select(Transaction).where(
        Transaction.sender_wallet_id == sender.id, Transaction.reference == reference))
    if prev:
        return prev

    amount = to_units(amount_str)
    debit, credit, fee = transfer_amounts(amount, fee_mode)
    target = find_wallet(db, recipient)
    if not target:
        raise RBCError("recipient_not_found", "Qabul qiluvchi topilmadi", 404)
    if target.id == sender.id:
        raise RBCError("self_transfer", "O'zingizga yuborib bo'lmaydi")
    if credit <= 0:
        raise RBCError("amount_too_small", "Miqdor komissiyadan keyin 0 dan katta bo'lishi kerak")

    # Deadlock bo'lmasligi uchun id tartibida qulflaymiz
    locked = {w.id: w for w in db.scalars(
        select(Wallet).where(Wallet.id.in_([sender.id, target.id])).order_by(Wallet.id).with_for_update())}
    s, t = locked[sender.id], locked[target.id]
    if s.status != "active" or t.status != "active":
        raise RBCError("wallet_blocked", "Hamyon faol emas", 403)
    if s.balance < debit:
        raise RBCError("insufficient_funds", "Balans yetarli emas")

    reserve = get_reserve(db)
    s.balance -= debit
    t.balance += credit
    reserve.balance += fee
    tx = Transaction(type="transfer", sender_wallet_id=s.id, receiver_wallet_id=t.id,
                     amount=credit, fee=fee, fee_mode=fee_mode, sender_debit=debit,
                     reference=reference)
    db.add(tx)
    db.flush()
    return tx


def test_faucet(db: Session, wallet: Wallet) -> Transaction:
    """FAQAT SINOV: haqiqiy deposit EMAS. Tranzaksiya 'test_faucet' deb belgilanadi."""
    if not config.TEST_FAUCET:
        raise RBCError("faucet_disabled", "Test faucet o'chirilgan", 403)
    amt = to_units(config.TEST_FAUCET_AMOUNT)
    w = db.scalar(select(Wallet).where(Wallet.id == wallet.id).with_for_update())
    if w.balance + amt > MAX_SUPPLY:
        raise RBCError("supply_limit", "Supply limiti", 400)
    w.balance += amt
    tx = Transaction(type="test_faucet", receiver_wallet_id=w.id, amount=amt,
                     reference=f"faucet-{secrets.token_hex(8)}")
    db.add(tx)
    db.flush()
    return tx

# ===== API =====
log = logging.getLogger("rbcoin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.JWT_SECRET or len(config.JWT_SECRET) < 32:
        raise RuntimeError("JWT_SECRET kamida 32 belgi bo'lishi kerak")
    if not config.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN o'rnatilmagan")
    # Sinov bosqichi: jadvallar avtomatik yaratiladi. Production'da Alembic ishlating.
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        get_reserve(db)
        db.commit()
    if config.PUBLIC_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/setWebhook", json={
                    "url": f"{config.PUBLIC_URL}/telegram/webhook",
                    "secret_token": config.WEBHOOK_SECRET or None,
                    "allowed_updates": ["message"],
                })
        except Exception:
            log.exception("Webhook o'rnatilmadi")
    yield


app = FastAPI(title="RB Coin API (sinov)", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS,
                   allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])


@app.exception_handler(RBCError)
async def rbc_error_handler(_: Request, e: RBCError):
    return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status)


def get_db():
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def current_wallet(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    if not authorization.startswith("Bearer "):
        raise RBCError("unauthorized", "Token yo'q", 401)
    try:
        payload = jwt.decode(authorization[7:], config.JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise RBCError("unauthorized", "Token yaroqsiz yoki muddati o'tgan", 401)
    user = db.get(User, int(payload["sub"]))
    if not user or user.status != "active":
        raise RBCError("blocked", "Akkaunt faol emas", 403)
    return user, db.scalar(select(Wallet).where(Wallet.user_id == user.id))


# ---------- Sog'liq tekshiruvi (Render health check) ----------
@app.get("/health")
def health():
    return {"ok": True}


# ---------- Auth ----------
class LoginIn(BaseModel):
    init_data: str


@app.post("/api/v1/auth/telegram")
def login(body: LoginIn, db: Session = Depends(get_db)):
    tg = verify_init_data(body.init_data, config.BOT_TOKEN)
    user, wallet = get_or_create_user(db, tg)
    db.commit()
    token = jwt.encode({"sub": str(user.id), "exp": int(time.time()) + config.JWT_TTL_SECONDS},
                       config.JWT_SECRET, algorithm="HS256")
    return {"token": token, "user": {"telegram_id": user.telegram_id, "first_name": user.first_name,
                                      "username": user.username}, "wallet_id": wallet.wallet_id}


# ---------- Wallet ----------
@app.get("/api/v1/wallet")
def wallet_info(ctx=Depends(current_wallet)):
    _, w = ctx
    return {"wallet_id": w.wallet_id, "balance": from_units(w.balance), "status": w.status,
            "network": "TEST", "notice": "Sinov rejimi: bu balans haqiqiy token emas."}


def _tx_out(t: Transaction, my_wallet_id: int):
    direction = "in" if t.receiver_wallet_id == my_wallet_id else "out"
    return {"id": t.id, "type": t.type, "direction": direction,
            "amount": from_units(t.amount), "fee": from_units(t.fee),
            "fee_mode": t.fee_mode, "status": t.status, "reference": t.reference,
            "created_at": t.created_at.isoformat()}


@app.get("/api/v1/transactions")
def history(limit: int = 30, ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    _, w = ctx
    rows = db.scalars(select(Transaction).where(
        or_(Transaction.sender_wallet_id == w.id, Transaction.receiver_wallet_id == w.id)
    ).order_by(Transaction.id.desc()).limit(min(max(limit, 1), 100)))
    return [_tx_out(t, w.id) for t in rows]


class TransferIn(BaseModel):
    recipient: str            # wallet ID (RB...) yoki Telegram ID
    amount: str               # satr: "12.5"
    fee_mode: str             # extra | included
    reference: str            # klient yaratgan unikal kalit (UUID)


@app.post("/api/v1/transfer")
def transfer(body: TransferIn, ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    _, w = ctx
    try:
        tx = do_transfer(db, w, body.recipient, body.amount, body.fee_mode, body.reference)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {**_tx_out(tx, w.id), "sender_debit": from_units(tx.sender_debit)}


@app.post("/api/v1/dev/faucet")
def faucet(ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    """FAQAT SINOV (TEST_FAUCET=1). Haqiqiy deposit emas."""
    _, w = ctx
    try:
        tx = test_faucet(db, w)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return _tx_out(tx, w.id)


# ---------- Telegram bot webhook ----------
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request,
                           x_telegram_bot_api_secret_token: str = Header(default="")):
    if config.WEBHOOK_SECRET and x_telegram_bot_api_secret_token != config.WEBHOOK_SECRET:
        return JSONResponse({"ok": False}, status_code=403)
    update = await request.json()
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if chat_id and text.startswith("/start") and config.WEBAPP_URL:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage", json={
                "chat_id": chat_id,
                "text": "RB Coin (sinov rejimi). Hamyonni ochish uchun tugmani bosing.",
                "reply_markup": {"inline_keyboard": [[{"text": "Open RB Coin",
                                                       "web_app": {"url": config.WEBAPP_URL}}]]},
            })
    return {"ok": True}
