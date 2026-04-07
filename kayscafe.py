import os
import logging
from datetime import datetime, timezone
from typing import Any

from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from google.cloud import firestore

# ============================================================
# CLOUD RUN TELEGRAM BOT FOR HOME CAFE (FIRESTORE VERSION)
# ============================================================
# Features:
# - Webhook mode for Cloud Run
# - Customer ordering flow
# - Receipt-style order confirmation
# - Admin-only commands to mark items sold out / available
# - Admin-only commands to add, hide, and edit items
# - Firestore database for menu, carts, and orders
#
# Environment variables:
# - BOT_TOKEN: Telegram bot token from BotFather
# - WEBHOOK_SECRET: random secret used in webhook URL path
# - ADMIN_USER_ID: your Telegram numeric user id
# - GCP_PROJECT: optional, Firestore project id if needed explicitly
#
# Firestore collections:
# - menu_items/{item_id}
# - carts/{user_id}
# - orders/{auto_id}
#
# Cloud Run notes:
# - Firestore is persistent, unlike /tmp SQLite.
# - The Cloud Run service account needs Firestore access.
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
raw_admin_ids = os.environ.get("ADMIN_USER_IDS", "").replace(";", ",")
ADMIN_USER_IDS = {
    int(x.strip())
    for x in raw_admin_ids.split(",")
    if x.strip()
}
GCP_PROJECT = os.environ.get("GCP_PROJECT", "kayscafe")
FIRESTORE_DATABASE = os.environ.get("FIRESTORE_DATABASE", "kayscafe")

db = firestore.Client(project=GCP_PROJECT, database=FIRESTORE_DATABASE)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()


# ============================================================
# DATABASE / FIRESTORE HELPERS
# ============================================================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS



def cents_to_money(cents: int) -> str:
    return f"${cents / 100:.2f}"



def seed_menu_items() -> None:
    items = [
        {
            "id": "hojicha_latte",
            "category": "Drinks",
            "name": "Hojicha Latte",
            "description": "Hojicha Latte Iced 280ml",
            "price_cents": 550,
            "available": True,
        },
        {
            "id": "matcha_latte",
            "category": "Drinks",
            "name": "Matcha Latte",
            "description": "Matcha Latte Iced 280ml",
            "price_cents": 600,
            "available": True,
        },
        {
            "id": "banana_pudding_matcha_latte",
            "category": "Drinks",
            "name": "Banana Pudding Matcha Latte",
            "description": "Matcha Latte topped with Banana Pudding and biscoff crumbs",
            "price_cents": 690,
            "available": False,
        },
        {
            "id": "banana_pudding_hojicha_latte",
            "category": "Drinks",
            "name": "Banana Pudding Hojicha Latte",
            "description": "Hojicha Latte topped with banana pudding and biscoff crumbs",
            "price_cents": 690,
            "available": False,
        },
        {
            "id": "strawberry_matcha",
            "category": "Drinks",
            "name": "Strawberry Matcha",
            "description": "Matcha Latte Iced with Strawberry Jam 280ml",
            "price_cents": 650,
            "available": True,
        },
        {
            "id": "strawberry_hojicha",
            "category": "Drinks",
            "name": "Strawberry Hojicha",
            "description": "Hojicha Latte with Strawberry Puree",
            "price_cents": 650,
            "available": True,
        },
        {
            "id": "banana_pudding_90g",
            "category": "Desserts",
            "name": "Banana Pudding (90g)",
            "description": "Only available on Friday, Saturday, Sunday while stocks last",
            "price_cents": 400,
            "available": False,
        },
    ]

    for item in items:
        ref = db.collection("menu_items").document(item["id"])
        if not ref.get().exists:
            item.setdefault("hidden", False)
            ref.set(item)



def fetch_categories() -> list[str]:
    docs = db.collection("menu_items").where("hidden", "==", False).stream()
    categories = sorted({doc.to_dict().get("category", "") for doc in docs if doc.to_dict().get("category")})
    return categories



def fetch_items_by_category(category: str) -> list[dict[str, Any]]:
    docs = (
        db.collection("menu_items")
        .where("category", "==", category)
        .where("hidden", "==", False)
        .stream()
    )
    items = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        items.append(data)
    items.sort(key=lambda x: x["name"])
    return items



def fetch_item(item_id: str) -> dict[str, Any] | None:
    doc = db.collection("menu_items").document(item_id).get()
    if not doc.exists:
        return None
    data = doc.to_dict()
    data["id"] = doc.id
    return data



def set_item_availability(item_id: str, available: bool) -> bool:
    ref = db.collection("menu_items").document(item_id)
    if not ref.get().exists:
        return False
    ref.update({"available": available})
    return True


def hide_item(item_id: str) -> bool:
    ref = db.collection("menu_items").document(item_id)
    if not ref.get().exists:
        return False
    ref.update({"hidden": True})
    return True


def add_item(item_id: str, category: str, name: str, price_cents: int, description: str) -> tuple[bool, str]:
    ref = db.collection("menu_items").document(item_id)
    if ref.get().exists:
        return False, "Item ID already exists."

    ref.set(
        {
            "id": item_id,
            "category": category,
            "name": name,
            "description": description,
            "price_cents": price_cents,
            "available": True,
            "hidden": False,
        }
    )
    return True, "Item added successfully."


def edit_item(item_id: str, new_name: str | None = None, new_price_cents: int | None = None) -> tuple[bool, str]:
    ref = db.collection("menu_items").document(item_id)
    snap = ref.get()
    if not snap.exists:
        return False, "Item ID not found."

    updates = {}
    if new_name is not None and new_name.strip():
        updates["name"] = new_name.strip()
    if new_price_cents is not None:
        if new_price_cents < 0:
            return False, "Price cannot be negative."
        updates["price_cents"] = new_price_cents

    if not updates:
        return False, "No valid fields to update."

    ref.update(updates)
    return True, "Item updated successfully."



def list_all_items_for_admin() -> str:
    docs = db.collection("menu_items").stream()
    items = []
    for doc in docs:
        data = doc.to_dict()
        data["id"] = doc.id
        items.append(data)

    items.sort(key=lambda x: (x["category"], x["name"]))

    lines = ["*Menu Item IDs*", "Use these IDs with /soldout or /available", ""]
    current_category = None
    for item in items:
        if item["category"] != current_category:
            current_category = item["category"]
            lines.append(f"*{current_category}*")
        status = "Available" if item["available"] else "Sold Out"
        if item.get("hidden"):
            status += ", Hidden"
        lines.append(
            f"- `{item['id']}` → {item['name']} ({cents_to_money(item['price_cents'])}) [{status}]"
        )
    return "\n".join(lines)



def get_cart_doc(user_id: int):
    return db.collection("carts").document(str(user_id))



def add_to_cart(user_id: int, item_id: str) -> None:
    item = fetch_item(item_id)
    if not item:
        raise ValueError("Item not found")

    ref = get_cart_doc(user_id)
    snap = ref.get()
    cart = snap.to_dict() if snap.exists else {"items": {}}
    items = cart.get("items", {})
    items[item_id] = int(items.get(item_id, 0)) + 1
    ref.set(
        {
            "user_id": user_id,
            "items": items,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )



def decrease_cart_item(user_id: int, item_id: str) -> None:
    ref = get_cart_doc(user_id)
    snap = ref.get()
    if not snap.exists:
        return

    cart = snap.to_dict()
    items = cart.get("items", {})
    if item_id not in items:
        return

    new_qty = int(items[item_id]) - 1
    if new_qty <= 0:
        del items[item_id]
    else:
        items[item_id] = new_qty

    ref.set(
        {
            "user_id": user_id,
            "items": items,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
    )



def clear_cart(user_id: int) -> None:
    get_cart_doc(user_id).delete()



def fetch_cart(user_id: int) -> list[dict[str, Any]]:
    snap = get_cart_doc(user_id).get()
    if not snap.exists:
        return []

    cart = snap.to_dict()
    raw_items = cart.get("items", {})
    rows: list[dict[str, Any]] = []

    for item_id, quantity in raw_items.items():
        item = fetch_item(item_id)
        if not item:
            continue
        rows.append(
            {
                "item_id": item_id,
                "quantity": int(quantity),
                "name": item["name"],
                "price_cents": int(item["price_cents"]),
                "available": bool(item["available"]),
            }
        )

    rows.sort(key=lambda x: x["name"])
    return rows



def cart_summary_text(user_id: int) -> tuple[str, int]:
    rows = fetch_cart(user_id)
    if not rows:
        return "Your cart is empty.", 0

    lines = ["🧾 *Your Cart*", ""]
    total_cents = 0

    for row in rows:
        line_total = row["quantity"] * row["price_cents"]
        total_cents += line_total
        sold_out_suffix = " _(now sold out)_" if not row["available"] else ""
        lines.append(
            f"- {row['name']} x{row['quantity']} = {cents_to_money(line_total)}{sold_out_suffix}"
        )

    lines.append("")
    lines.append(f"*Total:* {cents_to_money(total_cents)}")
    return "\n".join(lines), total_cents



def build_receipt(customer_name: str, order_id: str, items: list[dict[str, Any]], total_cents: int) -> str:
    lines = [
        "✅ *Order Confirmation*",
        f"Order ID: `{order_id}`",
        f"Customer: {customer_name}",
        "",
        "*Items Ordered*",
    ]

    for item in items:
        lines.append(
            f"- {item['name']} x{item['quantity']} @ {cents_to_money(item['unit_price_cents'])} = {cents_to_money(item['line_total_cents'])}"
        )

    lines.extend([
        "",
        f"*Total Price:* {cents_to_money(total_cents)}",
        "",
        "Thank you for your order!",
    ])
    return "\n".join(lines)



def create_order(user_id: int, customer_name: str) -> tuple[str, str]:
    rows = fetch_cart(user_id)
    if not rows:
        raise ValueError("Cart is empty")

    unavailable = [row["name"] for row in rows if not row["available"]]
    if unavailable:
        raise ValueError("These items are sold out: " + ", ".join(unavailable))

    items = []
    total_cents = 0
    for row in rows:
        item_total = row["quantity"] * row["price_cents"]
        total_cents += item_total
        items.append(
            {
                "item_id": row["item_id"],
                "name": row["name"],
                "quantity": row["quantity"],
                "unit_price_cents": row["price_cents"],
                "line_total_cents": item_total,
            }
        )

    order_ref = db.collection("orders").document()
    order_ref.set(
        {
            "user_id": user_id,
            "customer_name": customer_name,
            "items": items,
            "total_cents": total_cents,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "confirmed",
        }
    )

    clear_cart(user_id)
    return order_ref.id, build_receipt(customer_name, order_ref.id, items, total_cents)


# ============================================================
# KEYBOARDS
# ============================================================
def build_main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for category in fetch_categories():
        rows.append([InlineKeyboardButton(f"View {category}", callback_data=f"category:{category}")])
    rows.append([InlineKeyboardButton("View Cart", callback_data="view_cart")])
    rows.append([InlineKeyboardButton("Confirm Order", callback_data="confirm_order")])
    return InlineKeyboardMarkup(rows)



def build_category_keyboard(category: str) -> InlineKeyboardMarkup:
    rows = []
    items = fetch_items_by_category(category)

    for item in items:
        if item["available"]:
            rows.append([
                InlineKeyboardButton(
                    f"Add {item['name']} ({cents_to_money(item['price_cents'])})",
                    callback_data=f"add:{item['id']}",
                )
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"{item['name']} - Sold Out",
                    callback_data="sold_out",
                )
            ])

    rows.append([InlineKeyboardButton("View Cart", callback_data="view_cart")])
    rows.append([InlineKeyboardButton("Back to Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)



def build_cart_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    for row in fetch_cart(user_id):
        rows.append([
            InlineKeyboardButton(f"➖ {row['name']}", callback_data=f"minus:{row['item_id']}"),
            InlineKeyboardButton(f"Qty: {row['quantity']}", callback_data="noop"),
            InlineKeyboardButton(f"➕ {row['name']}", callback_data=f"add:{row['item_id']}"),
        ])

    rows.append([InlineKeyboardButton("Confirm Order", callback_data="confirm_order")])
    rows.append([InlineKeyboardButton("Clear Cart", callback_data="clear_cart")])
    rows.append([InlineKeyboardButton("Back to Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


# ============================================================
# TELEGRAM HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_first_name = update.effective_user.first_name or "Customer"
    text = (
        f"Hi {user_first_name}! Welcome to our home cafe.\n\n"
        "Please choose a category or view your cart."
    )
    await update.message.reply_text(text, reply_markup=build_main_menu_keyboard())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Please choose a category:",
        reply_markup=build_main_menu_keyboard(),
    )


async def cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    summary, _ = cart_summary_text(user_id)
    await update.message.reply_text(
        summary,
        parse_mode="Markdown",
        reply_markup=build_cart_keyboard(user_id),
    )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    await update.message.reply_text(list_all_items_for_admin(), parse_mode="Markdown")


async def soldout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /soldout <item_id>")
        return

    item_id = context.args[0].strip()
    ok = set_item_availability(item_id, False)
    if ok:
        await update.message.reply_text(f"Item `{item_id}` is now marked as sold out.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Item ID not found.")


async def available(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /available <item_id>")
        return

    item_id = context.args[0].strip()
    ok = set_item_availability(item_id, True)
    if ok:
        await update.message.reply_text(f"Item `{item_id}` is now available.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Item ID not found.")


async def hideitem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /hideitem <item_id>")
        return

    item_id = context.args[0].strip()
    ok = hide_item(item_id)
    if ok:
        await update.message.reply_text(f"Item `{item_id}` is now hidden from customers.", parse_mode="Markdown")
    else:
        await update.message.reply_text("Item ID not found.")


async def additem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    raw = update.message.text.replace("/additem", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) != 5:
        await update.message.reply_text(
            "Usage: /additem <item_id> | <category> | <name> | <price_cents> | <description>"
        )
        return

    item_id, category, name, price_str, description = parts

    try:
        price_cents = int(price_str)
        if price_cents < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("price_cents must be a non-negative integer.")
        return

    ok, message = add_item(item_id, category, name, price_cents, description)
    await update.message.reply_text(message)


async def edititem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("You are not allowed to use this command.")
        return

    raw = update.message.text.replace("/edititem", "", 1).strip()
    parts = [p.strip() for p in raw.split("|")]

    if len(parts) < 2 or len(parts) > 3:
        await update.message.reply_text(
            "Usage: /edititem <item_id> | <new_name> | <new_price_cents>"
            "You can leave <new_name> empty if you only want to change the price."
        )
        return

    item_id = parts[0]
    new_name = None
    new_price_cents = None

    if len(parts) >= 2 and parts[1] != "":
        new_name = parts[1]

    if len(parts) == 3 and parts[2] != "":
        try:
            new_price_cents = int(parts[2])
            if new_price_cents < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("new_price_cents must be a non-negative integer.")
            return

    ok, message = edit_item(item_id, new_name, new_price_cents)
    await update.message.reply_text(message)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    customer_name = query.from_user.full_name or "Customer"
    data = query.data

    if data == "main_menu":
        await query.edit_message_text(
            "Please choose a category or view your cart.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if data.startswith("category:"):
        category = data.split(":", 1)[1]
        items = fetch_items_by_category(category)
        lines = [f"*{category}*"]

        for item in items:
            status = "Available" if item["available"] else "Sold Out"
            lines.append(
                f"\n*{item['name']}*\n{item['description']}\n{cents_to_money(item['price_cents'])} · {status}"
            )

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=build_category_keyboard(category),
        )
        return

    if data == "sold_out":
        await query.answer("Sorry, this item is currently sold out.", show_alert=True)
        return

    if data.startswith("add:"):
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        if not item["available"]:
            await query.answer("This item is sold out.", show_alert=True)
            return

        add_to_cart(user_id, item_id)
        summary, _ = cart_summary_text(user_id)
        await query.edit_message_text(
            summary,
            parse_mode="Markdown",
            reply_markup=build_cart_keyboard(user_id),
        )
        return

    if data.startswith("minus:"):
        item_id = data.split(":", 1)[1]
        decrease_cart_item(user_id, item_id)
        summary, _ = cart_summary_text(user_id)
        await query.edit_message_text(
            summary,
            parse_mode="Markdown",
            reply_markup=build_cart_keyboard(user_id),
        )
        return

    if data == "view_cart":
        summary, _ = cart_summary_text(user_id)
        await query.edit_message_text(
            summary,
            parse_mode="Markdown",
            reply_markup=build_cart_keyboard(user_id),
        )
        return

    if data == "clear_cart":
        clear_cart(user_id)
        await query.edit_message_text(
            "Your cart has been cleared.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    if data == "confirm_order":
        try:
            order_id, receipt = create_order(user_id, customer_name)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        await query.edit_message_text(receipt, parse_mode="Markdown")

        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=message, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("Failed to notify admin %s: %s", admin_id, exc)
        return

    if data == "noop":
        return


# ============================================================
# FLASK ROUTES FOR CLOUD RUN
# ============================================================
@flask_app.get("/")
def healthcheck():
    return jsonify({"ok": True, "message": "Telegram cafe bot is running."})


@flask_app.post(f"/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)

        import asyncio

        async def process_update() -> None:
            await telegram_app.process_update(update)

        asyncio.run(process_update())
        return jsonify({"ok": True})
    except Exception as exc:
        logger.exception("Webhook processing failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500


# ============================================================
# STARTUP
# ============================================================
async def post_init(application: Application) -> None:
    logger.info("Telegram application initialized")


telegram_app.post_init = post_init
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(CommandHandler("menu", menu))
telegram_app.add_handler(CommandHandler("cart", cart))
telegram_app.add_handler(CommandHandler("adminmenu", admin_menu))
telegram_app.add_handler(CommandHandler("soldout", soldout))
telegram_app.add_handler(CommandHandler("available", available))
telegram_app.add_handler(CommandHandler("hideitem", hideitem))
telegram_app.add_handler(CommandHandler("additem", additem))
telegram_app.add_handler(CommandHandler("edititem", edititem))
telegram_app.add_handler(CallbackQueryHandler(button_handler))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is missing. Set it before deploying.")

seed_menu_items()

import asyncio
asyncio.run(telegram_app.initialize())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port, debug=True)


# ============================================================
# FILES TO CREATE BESIDE THIS SCRIPT
# ============================================================
# requirements.txt
# ----------------
# python-telegram-bot==22.5
# Flask==3.0.3
# gunicorn==22.0.0
# google-cloud-firestore==2.21.0
#
# Dockerfile
# ----------
# FROM python:3.11-slim
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 telegram_order_bot:flask_app
#
# ============================================================
# DEPLOYMENT STEPS
# ============================================================
# 1. Enable APIs:
# gcloud services enable run.googleapis.com cloudbuild.googleapis.com firestore.googleapis.com
#
# 2. Create Firestore database in Native mode in Google Cloud console.
#
# 3. Deploy:
# gcloud run deploy cafe-telegram-bot \
#   --source . \
#   --region asia-southeast1 \
#   --allow-unauthenticated \
#   --set-env-vars BOT_TOKEN=YOUR_TOKEN,WEBHOOK_SECRET=YOUR_SECRET,ADMIN_USER_ID=YOUR_TELEGRAM_ID
#
# 4. Grant Firestore access to the Cloud Run service account if needed.
# Usually role: Cloud Datastore User is enough for Firestore access.
#
# 5. Set webhook:
# curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://YOUR_CLOUD_RUN_URL/webhook/YOUR_SECRET"
#
# 6. Admin commands in Telegram:
# /adminmenu
# /soldout matcha_latte
# /available matcha_latte
# /hideitem matcha_latte
# /additem yuzu_matcha | Drinks | Yuzu Matcha | 720 | Matcha latte with yuzu foam
# /edititem matcha_latte | Premium Matcha Latte | 680
#
# ============================================================
# OPTIONAL NEXT IMPROVEMENTS
# ============================================================
# - Ask for pickup time before confirming order
# - Ask for phone number / delivery address
# - Add /showitem to unhide hidden items
# - Edit item description or category from Telegram
# - Store order status updates like preparing / ready / completed
# - Add separate admin group notifications
