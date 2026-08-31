import aiosqlite
from telegram import Update
from telegram.ext import ContextTypes

from database import (
    db_connection,
    get_user,
    get_bank_account,
    get_all_bank_accounts,
)
from utils.decorators import require_registered
from utils.helpers import fmt, now, parse_amount
from config import BANKS


def _bank_config(name: str):
    return next((bank for bank in BANKS if bank["name"] == name), None)


async def _choose_account(user_id: int, args: list[str]):
    accounts = await get_all_bank_accounts(user_id)
    if not accounts:
        return None, "❌ Tu n'as pas de compte bancaire. /ouvrir pour en créer un."

    if len(args) > 1:
        bank_name = " ".join(args[1:])
        account = await get_bank_account(user_id, bank_name)
        if not account:
            return None, f"❌ Aucun compte à la banque {bank_name}."
        return account, None

    if len(accounts) > 1:
        names = ", ".join(account["bank_name"] for account in accounts)
        message = "⚠️ Tu as plusieurs comptes. Précise la banque. "
        message += f"Comptes : {names}"
        return None, message

    return accounts[0], None


@require_registered
async def cmd_banques(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["🏦 Banques disponibles", ""]
    for bank in BANKS:
        lines.extend([
            f"🏛️ {bank['name']}",
            f"📈 Intérêts : {bank['interest'] * 100:.1f}%/jour",
            f"💰 Dépôt minimum : {fmt(bank['min'])}",
            f"💳 Prêt maximum : {fmt(bank['loan_max'])}",
            "",
        ])
    lines.append("👉 /ouvrir [nom banque] pour ouvrir un compte")
    await update.message.reply_text(chr(10).join(lines))


@require_registered
async def cmd_ouvrir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = await get_user(user.id)

    if not context.args:
        names = chr(10).join(f"• {bank['name']}" for bank in BANKS)
        message = "🏦 Choisis une banque :" + chr(10) + names
        message += chr(10) + chr(10) + "Usage : /ouvrir [Nom Banque]"
        await update.message.reply_text(message)
        return

    requested_name = " ".join(context.args)
    bank = next(
        (bank for bank in BANKS if bank["name"].lower() == requested_name.lower()),
        None,
    )
    if not bank:
        await update.message.reply_text("❌ Banque introuvable. Utilise /banques.")
        return

    existing = await get_bank_account(user.id, bank["name"])
    if existing:
        await update.message.reply_text("✅ Tu as déjà un compte dans cette banque.")
        return

    if user_data["balance"] < bank["min"]:
        await update.message.reply_text(
            f"❌ Cette banque exige au moins {fmt(bank['min'])} dans ton portefeuille."
        )
        return

    async with db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO bank_accounts "
                "(user_id, bank_name, balance, loan, loan_due, "
                "loan_penalty_applied, opened_at, last_interest) "
                "VALUES (?, ?, 0, 0, 0, 0, ?, ?)",
                (user.id, bank["name"], now(), now()),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            await update.message.reply_text("✅ Ce compte existe déjà.")
            return
        except Exception:
            await db.rollback()
            raise

    await update.message.reply_text(
        f"✅ Compte ouvert à la {bank['name']} ! "
        f"Intérêts : {bank['interest'] * 100:.1f}%/jour."
    )


@require_registered
async def cmd_depot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage : /depot montant [banque]")
        return

    user_data = await get_user(user.id)
    amount = parse_amount(context.args[0], user_data["balance"])
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Montant invalide.")
        return

    account, error = await _choose_account(user.id, context.args)
    if error:
        await update.message.reply_text(error)
        return

    async with db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            debit = await db.execute(
                "UPDATE users "
                "SET balance = balance - ?, total_spent = total_spent + ? "
                "WHERE user_id = ? AND balance >= ?",
                (amount, amount, user.id, amount),
            )
            if debit.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Fonds insuffisants.")
                return

            credit = await db.execute(
                "UPDATE bank_accounts SET balance = balance + ? "
                "WHERE user_id = ? AND bank_name = ?",
                (amount, user.id, account["bank_name"]),
            )
            if credit.rowcount != 1:
                raise RuntimeError("Compte bancaire introuvable pendant le dépôt")

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    new_bank_balance = account["balance"] + amount
    bank = _bank_config(account["bank_name"])
    daily_interest = int(
        new_bank_balance * (bank["interest"] if bank else 0)
    )
    lines = [
        "🏦 Dépôt réussi !",
        f"🏛️ Banque : {account['bank_name']}",
        f"💰 Montant : {fmt(amount)}",
        f"💵 Nouveau solde bancaire : {fmt(new_bank_balance)}",
        f"📈 Intérêts quotidiens estimés : +{fmt(daily_interest)}",
    ]
    await update.message.reply_text(chr(10).join(lines))


@require_registered
async def cmd_retrait(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage : /retrait montant [banque]")
        return

    amount = parse_amount(context.args[0])
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Montant invalide.")
        return

    account, error = await _choose_account(user.id, context.args)
    if error:
        await update.message.reply_text(error)
        return

    async with db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            debit = await db.execute(
                "UPDATE bank_accounts SET balance = balance - ? "
                "WHERE user_id = ? AND bank_name = ? AND balance >= ?",
                (amount, user.id, account["bank_name"], amount),
            )
            if debit.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Solde bancaire insuffisant.")
                return

            await db.execute(
                "UPDATE users "
                "SET balance = balance + ?, total_earned = total_earned + ? "
                "WHERE user_id = ?",
                (amount, amount, user.id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    lines = [
        "✅ Retrait réussi !",
        f"🏛️ Banque : {account['bank_name']}",
        f"💰 Montant : {fmt(amount)}",
        f"💵 Reste en banque : {fmt(account['balance'] - amount)}",
    ]
    await update.message.reply_text(chr(10).join(lines))


@require_registered
async def cmd_soldebanque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    accounts = await get_all_bank_accounts(update.effective_user.id)
    if not accounts:
        await update.message.reply_text("❌ Tu n'as aucun compte bancaire.")
        return

    total_balance = sum(account["balance"] for account in accounts)
    total_loan = sum(account["loan"] for account in accounts)
    lines = ["🏦 Tes comptes bancaires", ""]

    for account in accounts:
        bank = _bank_config(account["bank_name"])
        daily_interest = int(
            account["balance"] * (bank["interest"] if bank else 0)
        )
        lines.extend([
            f"🏛️ {account['bank_name']}",
            f"💰 Solde : {fmt(account['balance'])}",
            f"📈 Intérêts/jour : +{fmt(daily_interest)}",
            f"💳 Prêt actif : {fmt(account['loan'])}",
            "",
        ])

    lines.append(f"💎 Total bancaire : {fmt(total_balance)}")
    lines.append(f"💳 Total prêts : {fmt(total_loan)}")
    await update.message.reply_text(chr(10).join(lines))


@require_registered
async def cmd_pret(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_data = await get_user(user.id)

    if not context.args:
        await update.message.reply_text("Usage : /pret montant [banque]")
        return

    amount = parse_amount(context.args[0])
    if not amount or amount <= 0:
        await update.message.reply_text("❌ Montant invalide.")
        return

    account, error = await _choose_account(user.id, context.args)
    if error:
        await update.message.reply_text(error)
        return

    bank = _bank_config(account["bank_name"])
    if not bank:
        await update.message.reply_text("❌ Banque absente de la configuration.")
        return

    if amount > bank["loan_max"]:
        await update.message.reply_text(
            f"❌ Prêt maximum : {fmt(bank['loan_max'])}"
        )
        return

    credit_ok = (
        user_data.get("karma", 0) > -100
        and user_data.get("crimes_success", 0) < 20
    )
    if not credit_ok:
        await update.message.reply_text("❌ Prêt refusé : score de crédit insuffisant.")
        return

    debt = int(amount * 1.08)
    due_date = now() + 30 * 86400

    async with db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            reserve = await db.execute(
                "UPDATE bank_accounts "
                "SET loan = ?, loan_due = ?, loan_penalty_applied = 0 "
                "WHERE user_id = ? AND bank_name = ? AND loan = 0",
                (debt, due_date, user.id, account["bank_name"]),
            )
            if reserve.rowcount != 1:
                await db.rollback()
                await update.message.reply_text(
                    "❌ Un prêt est déjà actif sur ce compte."
                )
                return

            await db.execute(
                "UPDATE users "
                "SET balance = balance + ?, total_earned = total_earned + ? "
                "WHERE user_id = ?",
                (amount, amount, user.id),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    lines = [
        "💳 Prêt accordé !",
        f"💰 Montant reçu : {fmt(amount)}",
        f"📊 Dette avec intérêts : {fmt(debt)}",
        "📅 Échéance : 30 jours",
    ]
    await update.message.reply_text(chr(10).join(lines))


async def _find_active_loan(user_id: int, bank_name: str | None):
    accounts = await get_all_bank_accounts(user_id)
    active = [account for account in accounts if account["loan"] > 0]
    if not active:
        return None, "❌ Tu n'as pas de prêt actif."

    if bank_name:
        account = next(
            (
                account
                for account in active
                if account["bank_name"].lower() == bank_name.lower()
            ),
            None,
        )
        if not account:
            return None, "❌ Prêt introuvable pour cette banque."
        return account, None

    if len(active) > 1:
        names = ", ".join(account["bank_name"] for account in active)
        return None, f"⚠️ Précise la banque. Prêts actifs : {names}"

    return active[0], None


@require_registered
async def cmd_rembourser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    amount_arg = context.args[0] if context.args else None
    bank_name = " ".join(context.args[1:]) if len(context.args) > 1 else None
    account, error = await _find_active_loan(user.id, bank_name)
    if error:
        await update.message.reply_text(error)
        return

    async with db_connection() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")

            if (
                now() > account.get("loan_due", 0)
                and not account.get("loan_penalty_applied")
            ):
                await db.execute(
                    "UPDATE bank_accounts "
                    "SET loan = loan + CAST(loan * 0.05 AS INTEGER), "
                    "loan_penalty_applied = 1 "
                    "WHERE id = ? AND loan_penalty_applied = 0",
                    (account["id"],),
                )

            async with db.execute(
                "SELECT loan FROM bank_accounts WHERE id = ?",
                (account["id"],),
            ) as cursor:
                row = await cursor.fetchone()
            debt = row[0]

            if amount_arg is None:
                await db.commit()
                lines = [
                    f"💳 Dette à {account['bank_name']} : {fmt(debt)}",
                    "Usage : /rembourser montant [banque]",
                    "Ou : /rembourser tout [banque]",
                ]
                await update.message.reply_text(chr(10).join(lines))
                return

            user_data = await get_user(user.id)
            if amount_arg.lower() in ("tout", "all", "total"):
                requested = debt
            else:
                requested = parse_amount(amount_arg, user_data["balance"])

            if not requested or requested <= 0:
                await db.rollback()
                await update.message.reply_text("❌ Montant invalide.")
                return

            payment = min(requested, debt)
            debit = await db.execute(
                "UPDATE users "
                "SET balance = balance - ?, total_spent = total_spent + ? "
                "WHERE user_id = ? AND balance >= ?",
                (payment, payment, user.id, payment),
            )
            if debit.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Solde insuffisant.")
                return

            remaining = debt - payment
            await db.execute(
                "UPDATE bank_accounts "
                "SET loan = ?, "
                "loan_due = CASE WHEN ? = 0 THEN 0 ELSE loan_due END "
                "WHERE id = ?",
                (remaining, remaining, account["id"]),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    lines = [
        "✅ Remboursement effectué !",
        f"💰 Payé : {fmt(payment)}",
        f"💳 Reste dû : {fmt(remaining)}",
    ]
    await update.message.reply_text(chr(10).join(lines))


@require_registered
async def cmd_mescomptes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_soldebanque(update, context)


async def process_bank_interests():
    current_time = now()

    async with db_connection(row_factory=aiosqlite.Row) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT id, bank_name, balance, last_interest "
                "FROM bank_accounts "
                "WHERE balance > 0 AND last_interest > 0"
            ) as cursor:
                accounts = [dict(row) for row in await cursor.fetchall()]

            for account in accounts:
                bank = _bank_config(account["bank_name"])
                if not bank:
                    continue

                days_passed = max(
                    0,
                    (current_time - account["last_interest"]) // 86400,
                )
                if days_passed == 0:
                    continue

                interest_per_day = int(
                    account["balance"] * bank["interest"]
                )
                total_interest = max(0, interest_per_day * days_passed)
                next_interest_time = (
                    account["last_interest"] + days_passed * 86400
                )

                await db.execute(
                    "UPDATE bank_accounts "
                    "SET balance = balance + ?, last_interest = ? "
                    "WHERE id = ?",
                    (total_interest, next_interest_time, account["id"]),
                )

            await db.commit()
        except Exception:
            await db.rollback()
            raise