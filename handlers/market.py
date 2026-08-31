# handlers/market.py — Phase 1 : Marché joueur (commandes renommées)
# CORRECTION COMPLÈTE : Gestion robuste des items

import random
import aiosqlite
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from database import (
    DB_PATH, get_user, update_balance, add_life_journal,
    update_field, increment_field, log_company_action, db_connection
)
from utils.decorators import require_registered, require_free, cooldown
from utils.helpers import fmt, now, parse_amount, fmt_time, escape_html
from utils.aesthetics import card, alert, section
from handlers.vehicles import get_active_vehicle, get_vehicle_stats

# ─────────────────────────────────────────────────────────────────────────────
# Helper : obtenir la quantité d'un item dans l'inventaire
# ─────────────────────────────────────────────────────────────────────────────
async def get_item_quantity(user_id: int, item_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_id = ?",
            (user_id, item_id)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0

# ─────────────────────────────────────────────────────────────────────────────
# Helper : supprimer un item de l'inventaire (quantité)
# ─────────────────────────────────────────────────────────────────────────────
async def remove_item(user_id: int, item_id: int, quantity: int = 1) -> bool:
    qty = await get_item_quantity(user_id, item_id)
    if qty < quantity:
        return False
    async with aiosqlite.connect(DB_PATH) as db:
        if qty == quantity:
            await db.execute(
                "DELETE FROM inventory WHERE user_id = ? AND item_id = ?",
                (user_id, item_id)
            )
        else:
            await db.execute(
                "UPDATE inventory SET quantity = quantity - ? WHERE user_id = ? AND item_id = ?",
                (quantity, user_id, item_id)
            )
        await db.commit()
    return True

# ─────────────────────────────────────────────────────────────────────────────
# Helper : ajouter un item à l'inventaire (robuste)
# ─────────────────────────────────────────────────────────────────────────────
async def add_item(user_id: int, item_id: int, quantity: int, source: str = None):
    """Ajoute un item à l'inventaire d'un joueur. Si l'item n'existe pas, le crée."""
    # 1. S'assurer que l'item existe dans la table items
    await ensure_item_exists(item_id)
    
    # 2. Récupérer le nom de l'item
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name FROM items WHERE item_id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
        item_name = row[0] if row else f"Item #{item_id}"
        
        # 3. Ajouter ou mettre à jour dans l'inventaire
        async with db.execute(
            "SELECT quantity FROM inventory WHERE user_id = ? AND item_id = ?",
            (user_id, item_id)
        ) as cur2:
            existing = await cur2.fetchone()
        
        if existing:
            await db.execute(
                "UPDATE inventory SET quantity = quantity + ? WHERE user_id = ? AND item_id = ?",
                (quantity, user_id, item_id)
            )
        else:
            await db.execute(
                "INSERT INTO inventory (user_id, item_id, item_type, item_name, quantity, acquired_at) VALUES (?,?,?,?,?,?)",
                (user_id, item_id, source or "market", item_name, quantity, now())
            )
        await db.commit()

# ─────────────────────────────────────────────────────────────────────────────
# Helper : s'assurer qu'un item existe dans la table items (CORRIGÉ)
# ─────────────────────────────────────────────────────────────────────────────
async def ensure_item_exists(item_id: int) -> bool:
    """Vérifie et crée l'item dans la table items si nécessaire."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Vérifier si l'item existe déjà
        async with db.execute("SELECT item_id FROM items WHERE item_id = ?", (item_id,)) as cur:
            if await cur.fetchone():
                return True
        
        # L'item n'existe pas, on le crée
        # Essayer de récupérer le nom depuis l'inventaire
        async with db.execute(
            "SELECT item_name FROM inventory WHERE item_id = ? LIMIT 1",
            (item_id,)
        ) as cur2:
            inv_item = await cur2.fetchone()
        
        name = inv_item[0] if inv_item else f"Item #{item_id}"
        
        # Créer l'item avec un type par défaut
        await db.execute(
            """
            INSERT INTO items (item_id, name, type, rarity, value, emoji, description) 
            VALUES (?, ?, 'unknown', 'common', 0, '📦', ?)
            """,
            (item_id, name, f"Item récupéré automatiquement")
        )
        await db.commit()
        return True

# ─────────────────────────────────────────────────────────────────────────────
# Helper : calculer le bonus cargo pour les ventes
# ─────────────────────────────────────────────────────────────────────────────
async def get_cargo_bonus(user_id: int, base_price: int) -> tuple:
    active_vehicle = await get_active_vehicle(user_id)
    
    if active_vehicle:
        veh_data = await get_vehicle_stats(active_vehicle["veh_type"])
        vehicle_cargo = active_vehicle.get("cargo", 0)
        if vehicle_cargo == 0 and veh_data.get("cargo", 0) > 0:
            vehicle_cargo = veh_data.get("cargo", 0)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE vehicles SET cargo = ? WHERE veh_id = ?",
                    (vehicle_cargo, active_vehicle["veh_id"])
                )
                await db.commit()
    else:
        vehicle_cargo = 0
    
    cargo_bonus_mult = 1 + (vehicle_cargo / 500)
    cargo_bonus_mult = min(1.20, cargo_bonus_mult)
    final_price = int(base_price * cargo_bonus_mult)
    return final_price, cargo_bonus_mult, vehicle_cargo

# ─────────────────────────────────────────────────────────────────────────────
# /market : afficher les annonces
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    
    per_page = 10
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT ml.listing_id, ml.seller_id, ml.item_id, ml.quantity, ml.price,
                   i.name, i.emoji, i.rarity, u.full_name as seller_name
            FROM market_listings ml
            JOIN items i ON i.item_id = ml.item_id
            JOIN users u ON u.user_id = ml.seller_id
            WHERE ml.status = 'active' AND ml.expires_at > ?
            ORDER BY ml.price ASC, ml.created_at ASC
            LIMIT ? OFFSET ?
        """, (now(), per_page, (page - 1) * per_page)) as cur:
            listings = await cur.fetchall()
        
        async with db.execute("SELECT COUNT(*) FROM market_listings WHERE status = 'active' AND expires_at > ?", (now(),)) as cur2:
            total = (await cur2.fetchone())[0]
    
    if not listings:
        await update.message.reply_text(
            card("🏪 Marché joueur", ["Aucune annonce active pour le moment."],
                 icon="🏪", style="thick")
        )
        return
    
    text = f"🏪 **Marché joueur** (page {page}/{max(1, (total + per_page - 1) // per_page)})\n\n"
    for l in listings:
        text += (
            f"{l['emoji']} **{l['name']}** ({l['rarity']})\n"
            f"  Vendeur : {l['seller_name']}\n"
            f"  Quantité : {l['quantity']} | Prix unitaire : {fmt(l['price'])}\n"
            f"  ID annonce : `{l['listing_id']}`\n\n"
        )
    
    keyboard = []
    if page > 1:
        keyboard.append(InlineKeyboardButton("◀️ Page précédente", callback_data=f"market_page_{page-1}"))
    if (page * per_page) < total:
        keyboard.append(InlineKeyboardButton("Page suivante ▶️", callback_data=f"market_page_{page+1}"))
    
    if keyboard:
        reply_markup = InlineKeyboardMarkup([keyboard])
    else:
        reply_markup = None
    
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

async def market_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split("_")[-1])
    context.args = [str(page)]
    await cmd_market(update, context)
    try:
        await query.message.delete()
    except:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# /sellitem : mettre un item en vente
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
@require_free
@cooldown("sell_cooldown", 10, "⏳ Attends quelques secondes avant une autre vente.")
async def cmd_sellitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : /sellitem [item_id] [prix] [quantité]"
        )
        return

    try:
        item_id = int(context.args[0])
        price = int(context.args[1])
        quantity = int(context.args[2]) if len(context.args) > 2 else 1
        if item_id <= 0 or price <= 0 or quantity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Arguments invalides.")
        return

    await ensure_item_exists(item_id)
    final_price, cargo_mult, cargo = await get_cargo_bonus(user.id, price)
    expires_at = now() + 7 * 86400

    async with db_connection(row_factory=aiosqlite.Row) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute(
                "SELECT name, emoji FROM items WHERE item_id=?",
                (item_id,),
            ) as cursor:
                item = await cursor.fetchone()
            if not item:
                await db.rollback()
                await update.message.reply_text("❌ Item introuvable.")
                return

            reserve = await db.execute(
                "UPDATE inventory SET quantity=quantity-? "
                "WHERE user_id=? AND item_id=? AND quantity>=?",
                (quantity, user.id, item_id, quantity),
            )
            if reserve.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Stock insuffisant.")
                return

            await db.execute(
                "DELETE FROM inventory WHERE user_id=? AND item_id=? AND quantity=0",
                (user.id, item_id),
            )
            cursor = await db.execute(
                "INSERT INTO market_listings "
                "(seller_id,item_id,quantity,price,created_at,expires_at,status) "
                "VALUES (?,?,?,?,?,?,'active')",
                (user.id, item_id, quantity, final_price, now(), expires_at),
            )
            listing_id = cursor.lastrowid
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    bonus = int((cargo_mult - 1) * 100)
    lines = [
        "📦 Item mis en vente !",
        f"{item['emoji']} {item['name']}",
        f"Quantité : {quantity}",
        f"Prix unitaire : {fmt(final_price)}",
        f"Annonce : #{listing_id}",
    ]
    if bonus > 0:
        lines.append(f"📦 Bonus cargo : +{bonus}%")
    await update.message.reply_text(chr(10).join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# /cancelitem : annuler une vente (CORRIGÉ)
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
@require_free
async def cmd_cancelitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.message.reply_text("Usage : /cancelitem [listing_id]")
        return

    try:
        listing_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return

    async with db_connection(row_factory=aiosqlite.Row) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT ml.*, i.name FROM market_listings ml "
                "JOIN items i ON i.item_id=ml.item_id "
                "WHERE ml.listing_id=? AND ml.seller_id=? "
                "AND ml.status='active'",
                (listing_id, user.id),
            ) as cursor:
                listing = await cursor.fetchone()
            if not listing:
                await db.rollback()
                await update.message.reply_text("❌ Annonce introuvable ou déjà traitée.")
                return

            claim = await db.execute(
                "UPDATE market_listings SET status='cancelled',quantity=0 "
                "WHERE listing_id=? AND status='active'",
                (listing_id,),
            )
            if claim.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Annonce déjà traitée.")
                return

            await db.execute(
                "INSERT INTO inventory "
                "(user_id,item_id,item_type,item_name,quantity,acquired_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id,item_id) DO UPDATE SET "
                "quantity=inventory.quantity+excluded.quantity",
                (
                    user.id,
                    listing["item_id"],
                    "market_return",
                    listing["name"],
                    listing["quantity"],
                    now(),
                ),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    await update.message.reply_text(
        f"✅ Annonce annulée. {listing['quantity']} objet(s) récupéré(s)."
    )

# ─────────────────────────────────────────────────────────────────────────────
# /buyitem : acheter un item
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
@require_free
@cooldown("buy_cooldown", 5, "⏳ Attends quelques secondes avant un autre achat.")
async def cmd_buyitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buyer = update.effective_user

    if not context.args:
        await update.message.reply_text("Usage : /buyitem [listing_id] [quantité]")
        return

    try:
        listing_id = int(context.args[0])
        quantity = int(context.args[1]) if len(context.args) > 1 else 1
        if listing_id <= 0 or quantity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Arguments invalides.")
        return

    async with db_connection(row_factory=aiosqlite.Row) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")

            async with db.execute(
                "SELECT ml.*, i.name, i.emoji, i.rarity "
                "FROM market_listings ml "
                "JOIN items i ON i.item_id=ml.item_id "
                "WHERE ml.listing_id=? AND ml.status='active' "
                "AND ml.expires_at>?",
                (listing_id, now()),
            ) as cursor:
                listing = await cursor.fetchone()

            if not listing:
                await db.rollback()
                await update.message.reply_text("❌ Annonce indisponible.")
                return
            if listing["seller_id"] == buyer.id:
                await db.rollback()
                await update.message.reply_text("❌ Tu ne peux pas acheter ta propre annonce.")
                return
            if listing["quantity"] < quantity:
                await db.rollback()
                await update.message.reply_text("❌ Quantité insuffisante dans l'annonce.")
                return

            total = listing["price"] * quantity
            debit = await db.execute(
                "UPDATE users SET balance=balance-?, total_spent=total_spent+? "
                "WHERE user_id=? AND balance>=?",
                (total, total, buyer.id, total),
            )
            if debit.rowcount != 1:
                await db.rollback()
                await update.message.reply_text("❌ Fonds insuffisants.")
                return

            await db.execute(
                "UPDATE users SET balance=balance+?, total_earned=total_earned+? "
                "WHERE user_id=?",
                (total, total, listing["seller_id"]),
            )

            remaining = listing["quantity"] - quantity
            if remaining == 0:
                claim = await db.execute(
                    "UPDATE market_listings SET quantity=0,status='sold' "
                    "WHERE listing_id=? AND status='active' AND quantity=?",
                    (listing_id, listing["quantity"]),
                )
            else:
                claim = await db.execute(
                    "UPDATE market_listings SET quantity=? "
                    "WHERE listing_id=? AND status='active' AND quantity=?",
                    (remaining, listing_id, listing["quantity"]),
                )
            if claim.rowcount != 1:
                raise RuntimeError("Annonce modifiée pendant l'achat")

            await db.execute(
                "INSERT INTO inventory "
                "(user_id,item_id,item_type,item_name,quantity,acquired_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id,item_id) DO UPDATE SET "
                "quantity=inventory.quantity+excluded.quantity",
                (
                    buyer.id,
                    listing["item_id"],
                    "market",
                    listing["name"],
                    quantity,
                    now(),
                ),
            )

            await db.execute(
                "UPDATE company_products SET "
                "sales=sales+?, revenue=revenue+? "
                "WHERE name=?",
                (quantity, total, listing["name"]),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    await add_life_journal(
        buyer.id,
        "market",
        f"Achat de {quantity}x {listing['name']} pour {fmt(total)}",
        severity="success",
    )
    lines = [
        "✅ Achat réussi !",
        f"{listing['emoji']} {listing['name']} ({listing['rarity']})",
        f"Quantité : {quantity}",
        f"Prix total : {fmt(total)}",
    ]
    await update.message.reply_text(chr(10).join(lines))

# ─────────────────────────────────────────────────────────────────────────────
# /useitem : utiliser un item (consommable)
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
@require_free
@cooldown("use_cooldown", 2, "⏳ Attends 2 secondes avant d'utiliser un autre item.")
async def cmd_useitem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = await get_user(user.id)
    
    if not context.args:
        await update.message.reply_text(
            "Usage : <code>/useitem [item_id] [quantité]</code>\n\n"
            "Les items consommables ont des effets :\n"
            "• <code>heal</code> : restaure des points de santé\n"
            "• <code>energy</code> : restaure de l'énergie\n"
            "• <code>xp</code> : donne de l'XP\n"
            "• <code>money</code> : donne de l'argent\n"
            "• <code>buff</code> : boost temporaire (bonheur +, stress -)\n\n"
            "Exemple : <code>/useitem 5</code> (utilise 1 exemplaire de l'item #5)",
            parse_mode="HTML"
        )
        return
    
    try:
        item_id = int(context.args[0])
        quantity = int(context.args[1]) if len(context.args) > 1 else 1
        if quantity <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Arguments invalides.")
        return
    
    current_qty = await get_item_quantity(user.id, item_id)
    if current_qty < quantity:
        await update.message.reply_text(f"❌ Tu ne possèdes que {current_qty} exemplaire(s) de cet item.")
        return
    
    # S'assurer que l'item existe dans la table items
    await ensure_item_exists(item_id)
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT name, emoji, rarity, type, effect_type, effect_value FROM items WHERE item_id = ?",
            (item_id,)
        ) as cur:
            item = await cur.fetchone()
        if not item:
            await update.message.reply_text("❌ Item introuvable.")
            return
    
    if item["type"] != "consumable":
        await update.message.reply_text(f"❌ L'item {item['emoji']} <b>{escape_html(item['name'])}</b> n'est pas consommable.", parse_mode="HTML")
        return
    
    effect_type = item["effect_type"]
    effect_value = item["effect_value"] * quantity
    
    effect_msg = ""
    if effect_type == "heal":
        new_health = min(100, u["health"] + effect_value)
        await update_field(user.id, "health", new_health)
        effect_msg = f"❤️ Santé : {u['health']} → {new_health}"
    elif effect_type == "energy":
        new_energy = min(100, u["energy"] + effect_value)
        await update_field(user.id, "energy", new_energy)
        effect_msg = f"⚡ Énergie : {u['energy']} → {new_energy}"
    elif effect_type == "xp":
        await increment_field(user.id, "xp", effect_value)
        effect_msg = f"✨ +{effect_value} XP"
    elif effect_type == "money":
        await update_balance(user.id, effect_value)
        effect_msg = f"💰 +{fmt(effect_value)} coins"
    elif effect_type == "buff":
        new_happiness = min(100, u["happiness"] + effect_value)
        new_stress = max(0, u["stress"] - effect_value // 2)
        await update_field(user.id, "happiness", new_happiness)
        await update_field(user.id, "stress", new_stress)
        effect_msg = f"✨ Buff appliqué : Bonheur +{effect_value}, Stress -{effect_value//2}"
    else:
        await update.message.reply_text(f"❌ Effet '{effect_type}' non implémenté pour le moment.")
        return
    
    await remove_item(user.id, item_id, quantity)
    
    await add_life_journal(
        user.id, "item",
        f"Utilisation de {quantity}x {item['emoji']} {item['name']} : {effect_msg}",
        severity="success"
    )
    
    await update.message.reply_text(
        f"✨ <b>Utilisation de {item['emoji']} {escape_html(item['name'])}</b> (x{quantity})\n\n"
        f"{effect_msg}\n\n"
        f"Il te reste {current_qty - quantity} exemplaire(s) de cet item.",
        parse_mode="HTML"
    )

# ─────────────────────────────────────────────────────────────────────────────
# /myitems : voir ses propres annonces
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
async def cmd_myitems(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT ml.listing_id, ml.item_id, ml.quantity, ml.price, ml.created_at, ml.expires_at,
                   i.name, i.emoji, i.rarity
            FROM market_listings ml
            JOIN items i ON i.item_id = ml.item_id
            WHERE ml.seller_id = ? AND ml.status = 'active'
            ORDER BY ml.created_at DESC
        """, (user.id,)) as cur:
            listings = await cur.fetchall()
    
    if not listings:
        await update.message.reply_text("📭 Tu n'as aucune annonce active.")
        return
    
    text = "📦 <b>Tes annonces</b>\n\n"
    for l in listings:
        expires_in = l["expires_at"] - now()
        text += (
            f"ID: <code>{l['listing_id']}</code> — {l['emoji']} <b>{escape_html(l['name'])}</b> ({l['rarity']})\n"
            f"  Quantité : {l['quantity']} | Prix unitaire : {fmt(l['price'])}\n"
            f"  Expire dans : {fmt_time(expires_in)}\n\n"
        )
    text += "Pour annuler : <code>/cancelitem [listing_id]</code>"
    await update.message.reply_text(text, parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────────────────
# /cargobonus : afficher le bonus de cargo actuel
# ─────────────────────────────────────────────────────────────────────────────
@require_registered
async def cmd_cargobonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le bonus de cargo actuel du véhicule."""
    user = update.effective_user
    
    active_vehicle = await get_active_vehicle(user.id)
    
    if not active_vehicle:
        await update.message.reply_text(
            "🚫 <b>Aucun véhicule actif</b>\n\n"
            "Utilise <code>/garage</code> pour sélectionner un véhicule actif.\n"
            "Le bonus cargo s'applique sur les ventes d'items.",
            parse_mode="HTML"
        )
        return
    
    veh_data = await get_vehicle_stats(active_vehicle["veh_type"])
    vehicle_cargo = active_vehicle.get("cargo", 0)
    
    if vehicle_cargo == 0 and veh_data.get("cargo", 0) > 0:
        vehicle_cargo = veh_data.get("cargo", 0)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE vehicles SET cargo = ? WHERE veh_id = ?",
                (vehicle_cargo, active_vehicle["veh_id"])
            )
            await db.commit()
    
    vehicle_name = active_vehicle.get("veh_type", "Véhicule")
    
    cargo_bonus_mult = 1 + (vehicle_cargo / 500)
    cargo_bonus_mult = min(1.20, cargo_bonus_mult)
    bonus_pct = int((cargo_bonus_mult - 1) * 100)
    
    if vehicle_cargo >= 80:
        level = "🏆 Maximale"
        desc = "Vous profitez du bonus maximum sur vos ventes !"
    elif vehicle_cargo >= 50:
        level = "✅ Élevée"
        desc = "Bon bonus sur vos ventes. Les missions de livraison lourdes sont accessibles."
    elif vehicle_cargo >= 30:
        level = "📦 Moyenne"
        desc = "Bonus modéré sur les ventes. Envisagez un véhicule avec plus de cargo."
    elif vehicle_cargo >= 10:
        level = "🌱 Faible"
        desc = "Bonus limité. Un véhicule avec plus de cargo augmenterait vos profits."
    else:
        level = "⚠️ Minimale"
        desc = "Presque pas de bonus. Un véhicule utilitaire serait plus rentable."
    
    text = (
        f"📦 <b>Bonus de Cargo</b>\n\n"
        f"🚗 Véhicule : <b>{escape_html(vehicle_name)}</b>\n"
        f"📦 Cargo : <b>{vehicle_cargo}/100</b>\n"
        f"📊 Niveau : {level}\n"
        f"💰 Bonus sur les ventes : <b>+{bonus_pct}%</b>\n"
        f"📈 Multiplicateur : <b>x{cargo_bonus_mult:.2f}</b>\n\n"
        f"_{desc}_\n\n"
        f"💡 Les véhicules avec un cargo élevé augmentent vos profits lors des ventes sur le marché."
    )
    await update.message.reply_text(text, parse_mode="HTML")

# ─────────────────────────────────────────────────────────────────────────────
# Maintenance : nettoyer les annonces expirées
# ─────────────────────────────────────────────────────────────────────────────
async def clean_expired_listings():
    current = now()
    async with db_connection(row_factory=aiosqlite.Row) as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT ml.*, i.name FROM market_listings ml "
                "JOIN items i ON i.item_id=ml.item_id "
                "WHERE ml.status='active' AND ml.expires_at<=?",
                (current,),
            ) as cursor:
                listings = await cursor.fetchall()

            for listing in listings:
                claim = await db.execute(
                    "UPDATE market_listings SET status='expired',quantity=0 "
                    "WHERE listing_id=? AND status='active'",
                    (listing["listing_id"],),
                )
                if claim.rowcount != 1:
                    continue
                await db.execute(
                    "INSERT INTO inventory "
                    "(user_id,item_id,item_type,item_name,quantity,acquired_at) "
                    "VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(user_id,item_id) DO UPDATE SET "
                    "quantity=inventory.quantity+excluded.quantity",
                    (
                        listing["seller_id"],
                        listing["item_id"],
                        "market_return",
                        listing["name"],
                        listing["quantity"],
                        current,
                    ),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise