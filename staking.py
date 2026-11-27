"""RB Coin - Staking + Earn (SINOV). main.py oxiriga ulanadi (`import staking`).
Yangi jadvallar (stake_plans, reward_pool, stakes) ishga tushganda avtomatik yaratiladi."""
import os
from datetime import timedelta, timezone

from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from fastapi import Depends
from main import (Base, RBCError, Transaction, Wallet, app, current_wallet,
                  from_units, get_db, now, to_units)

# ---- Sozlamalar (sinov qiymatlari; keyin admin panelga ko'chadi) ----
STAKE_MIN = os.getenv("STAKE_MIN", "1")                    # minimal miqdor, RBC
TEST_REWARD_POOL = os.getenv("TEST_REWARD_POOL", "1000000")  # SINOV mukofot puli, RBC
# (muddat kun, butun muddat uchun mukofot bps; 100 bps = 1%)
DEFAULT_PLANS = [(7, 30), (30, 150), (90, 500), (180, 1200)]
EARLY_RULE = ("Muddatidan oldin yechilsa, asosiy summa to'liq qaytariladi, "
              "lekin mukofot berilmaydi.")
NOTICE = ("Sinov rejimi: stavkalar sinov uchun belgilangan, daromad kafolatlanmagan. "
          "Mukofot faqat mukofot puli doirasida beriladi.")


def calc_reward(amount: int, bps: int) -> int:
    """Butun muddat uchun mukofot, pastga yaxlitlanadi."""
    return amount * bps // 10000


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---- Modellar ----
class StakePlan(Base):
    __tablename__ = "stake_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    days: Mapped[int] = mapped_column(Integer, unique=True)
    reward_bps: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RewardPool(Base):
    __tablename__ = "reward_pool"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)


class Stake(Base):
    __tablename__ = "stakes"
    __table_args__ = (UniqueConstraint("wallet_id", "reference"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wallet_id: Mapped[int] = mapped_column(ForeignKey("wallets.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    days: Mapped[int] = mapped_column(Integer)
    reward_bps: Mapped[int] = mapped_column(Integer)
    reward: Mapped[int] = mapped_column(BigInteger)          # shu stake uchun band qilingan mukofot
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | completed | cancelled
    reference: Mapped[str] = mapped_column(String(64))
    start_at = mapped_column(DateTime(timezone=True))
    end_at = mapped_column(DateTime(timezone=True))
    closed_at = mapped_column(DateTime(timezone=True), nullable=True)


# ---- Yordamchilar ----
_seeded = False


def ensure_seed(db: Session):
    """Standart muddatlar va SINOV mukofot pulini bir marta yaratadi."""
    global _seeded
    if _seeded:
        return
    try:
        if not db.scalar(select(StakePlan.id).limit(1)):
            for days, bps in DEFAULT_PLANS:
                db.add(StakePlan(days=days, reward_bps=bps))
        if not db.get(RewardPool, 1):
            db.add(RewardPool(id=1, balance=to_units(TEST_REWARD_POOL)))
        db.commit()
    except IntegrityError:
        db.rollback()          # boshqa so'rov allaqachon yaratgan
    except Exception:
        db.rollback()
        raise
    _seeded = True


def get_pool(db: Session, lock: bool = False) -> RewardPool:
    q = select(RewardPool).where(RewardPool.id == 1)
    return db.scalar(q.with_for_update() if lock else q)


def lock_wallet(db: Session, wallet_id: int) -> Wallet:
    return db.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())


def stake_out(s: Stake) -> dict:
    end = aware(s.end_at)
    return {"id": s.id, "amount": from_units(s.amount), "days": s.days,
            "reward_percent": f"{s.reward_bps / 100:g}", "reward": from_units(s.reward),
            "status": s.status, "start_at": aware(s.start_at).isoformat(),
            "end_at": end.isoformat(), "matured": s.status == "active" and now() >= end}


class StakeIn(BaseModel):
    days: int
    amount: str
    reference: str


# ---- Biznes mantiq (qulf tartibi har doim: stake -> wallet -> pool) ----
def _create_stake(db: Session, wallet: Wallet, body: StakeIn) -> Stake:
    ensure_seed(db)
    ref = (body.reference or "").strip()
    if not ref or len(ref) > 64:
        raise RBCError("bad_reference", "reference majburiy, maks. 64 belgi")
    prev = db.scalar(select(Stake).where(Stake.wallet_id == wallet.id, Stake.reference == ref))
    if prev:                                   # idempotency
        return prev
    amount = to_units(body.amount)
    if amount < to_units(STAKE_MIN):
        raise RBCError("below_min", f"Minimal staking miqdori: {STAKE_MIN} RBC")
    plan = db.scalar(select(StakePlan).where(StakePlan.days == body.days, StakePlan.active.is_(True)))
    if not plan:
        raise RBCError("bad_plan", "Bunday muddat mavjud emas", 404)
    reward = calc_reward(amount, plan.reward_bps)

    w = lock_wallet(db, wallet.id)
    if w.status != "active":
        raise RBCError("wallet_blocked", "Hamyon faol emas", 403)
    if w.balance < amount:
        raise RBCError("insufficient_funds", "Balans yetarli emas")
    pool = get_pool(db, lock=True)
    if pool.balance < reward:
        raise RBCError("pool_empty", "Mukofot puli yetarli emas. Kichikroq miqdor yoki qisqaroq muddat tanlang")

    start = now()
    s = Stake(wallet_id=w.id, amount=amount, days=plan.days, reward_bps=plan.reward_bps,
              reward=reward, reference=ref, start_at=start, end_at=start + timedelta(days=plan.days))
    w.balance -= amount
    pool.balance -= reward                     # mukofot oldindan band qilinadi
    db.add(s)
    db.flush()
    db.add(Transaction(type="stake_lock", sender_wallet_id=w.id, amount=amount,
                       sender_debit=amount, reference=f"stake-{s.id}-lock"))
    db.flush()
    return s


def _own_stake(db: Session, wallet: Wallet, stake_id: int) -> Stake:
    s = db.scalar(select(Stake).where(Stake.id == stake_id, Stake.wallet_id == wallet.id).with_for_update())
    if not s:
        raise RBCError("not_found", "Staking topilmadi", 404)
    if s.status != "active":
        raise RBCError("already_closed", "Bu staking allaqachon yopilgan")
    return s


def _claim(db: Session, wallet: Wallet, stake_id: int) -> Stake:
    s = _own_stake(db, wallet, stake_id)
    if now() < aware(s.end_at):
        raise RBCError("not_matured", "Muddat hali tugamagan")
    w = lock_wallet(db, wallet.id)
    payout = s.amount + s.reward
    w.balance += payout
    s.status, s.closed_at = "completed", now()
    db.add(Transaction(type="stake_payout", receiver_wallet_id=w.id, amount=payout,
                       reference=f"stake-{s.id}-payout"))
    db.flush()
    return s


def _early_unstake(db: Session, wallet: Wallet, stake_id: int) -> Stake:
    s = _own_stake(db, wallet, stake_id)
    if now() >= aware(s.end_at):
        raise RBCError("matured", "Muddat tugagan, mukofot bilan birga oling")
    w = lock_wallet(db, wallet.id)
    pool = get_pool(db, lock=True)
    w.balance += s.amount                      # asosiy summa qaytadi
    pool.balance += s.reward                   # band qilingan mukofot pulga qaytadi
    s.status, s.closed_at = "cancelled", now()
    db.add(Transaction(type="stake_payout", receiver_wallet_id=w.id, amount=s.amount,
                       reference=f"stake-{s.id}-early"))
    db.flush()
    return s


def _run(db: Session, fn, *args) -> dict:
    try:
        s = fn(db, *args)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return stake_out(s)


# ---- API ----
@app.get("/api/v1/earn/plans")
def earn_plans(ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    ensure_seed(db)
    plans = db.scalars(select(StakePlan).where(StakePlan.active.is_(True)).order_by(StakePlan.days)).all()
    return {"plans": [{"days": p.days, "reward_percent": f"{p.reward_bps / 100:g}", "reward_bps": p.reward_bps}
                      for p in plans],
            "min_stake": STAKE_MIN, "pool_balance": from_units(get_pool(db).balance),
            "early_rule": EARLY_RULE, "notice": NOTICE}


@app.get("/api/v1/earn/stakes")
def earn_stakes(ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    _, w = ctx
    rows = db.scalars(select(Stake).where(Stake.wallet_id == w.id).order_by(Stake.id.desc()).limit(50))
    return [stake_out(s) for s in rows]


@app.post("/api/v1/earn/stake")
def earn_stake(body: StakeIn, ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    return _run(db, _create_stake, ctx[1], body)


@app.post("/api/v1/earn/stakes/{stake_id}/claim")
def earn_claim(stake_id: int, ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    return _run(db, _claim, ctx[1], stake_id)


@app.post("/api/v1/earn/stakes/{stake_id}/unstake")
def earn_unstake(stake_id: int, ctx=Depends(current_wallet), db: Session = Depends(get_db)):
    return _run(db, _early_unstake, ctx[1], stake_id)
