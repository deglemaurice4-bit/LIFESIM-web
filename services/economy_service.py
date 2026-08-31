from database import get_user, update_balance, increment_field, update_field, now
from utils.helpers import fmt, get_karma_multiplier, fmt_time
from utils.simulation import compute_condition_penalties
from config import JOBS, DIPLOME_SALARY_BONUS, DAILY_MIN, DAILY_MAX
import random

class FakeUpdate:
    def __init__(self, user_id, full_name):
        self.effective_user = type('', (), {'id': user_id, 'full_name': full_name})()
        self.message = type('', (), {'reply_text': lambda self, text, **kwargs: None})()

class FakeContext:
    def __init__(self):
        self.bot = None

async def do_work(user_id: int) -> dict:
    # Réutilisation de la logique de cmd_travailler
    u = await get_user(user_id)
    penalties = compute_condition_penalties(u)

    if u.get("energy", 100) < 20:
        raise ValueError("Énergie insuffisante")
    if u.get("hunger", 100) < 12:
        raise ValueError("Faim trop élevée")
    if u.get("health", 100) < 15:
        raise ValueError("Santé trop basse")

    job = u.get("job", "Livreur")
    job_data = JOBS.get(job, JOBS["Livreur"])
    dip_bonus = DIPLOME_SALARY_BONUS.get(u.get("diplome", ""), 1.0)
    karma_mult = get_karma_multiplier(u.get("karma", 0))

    base = random.randint(job_data["min"], job_data["max"])
    amount = int(base * dip_bonus * karma_mult * penalties["productivity_mult"])

    energy_cost = random.randint(15, 25) + (5 if u.get("stress", 0) > 70 else 0)
    new_energy = max(0, u.get("energy", 100) - energy_cost)
    new_stress = min(100, u.get("stress", 0) + random.randint(3, 8))
    new_hunger = max(0, u.get("hunger", 100) - 10)
    new_health = max(0, u.get("health", 100) - (4 if penalties["severe_combo"] else 0))

    await update_balance(user_id, amount)
    await update_field(user_id, "work_last", now())
    await update_field(user_id, "energy", new_energy)
    await update_field(user_id, "stress", new_stress)
    await update_field(user_id, "hunger", new_hunger)
    await update_field(user_id, "health", new_health)
    await increment_field(user_id, "xp", job_data["xp"])

    return {
        "balance": (await get_user(user_id))["balance"],
        "xp": (await get_user(user_id))["xp"],
        "energy": new_energy,
        "stress": new_stress,
        "hunger": new_hunger,
        "health": new_health,
        "earned": amount,
    }

async def do_daily(user_id: int) -> dict:
    u = await get_user(user_id)
    penalties = compute_condition_penalties(u)
    karma_mult = get_karma_multiplier(u.get("karma", 0))
    level_bonus = 1 + (u.get("level", 1) - 1) * 0.01
    base = random.randint(DAILY_MIN, DAILY_MAX)
    amount = int(base * karma_mult * level_bonus * penalties["passive_income_mult"])

    await update_balance(user_id, amount)
    await update_field(user_id, "daily_last", now())
    await increment_field(user_id, "xp", 30)

    return {
        "balance": (await get_user(user_id))["balance"],
        "xp": (await get_user(user_id))["xp"],
        "daily_bonus": amount,
    }