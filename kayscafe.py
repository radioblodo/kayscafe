import os
import json
import sqlite3
import logging
import asyncio
import threading
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

# ============================================================
# RAILWAY TELEGRAM BOT FOR HOME CAFE (SQLITE VERSION)
# ============================================================
# Features:
# - Webhook mode for Railway
# - Customer ordering flow
# - Receipt-style order confirmation
# - Button-based admin actions for sold out, available, hide, remove, rename, price change, and add item
# - SQLite database for menu, carts, and orders
#
# Environment variables:
# - BOT_TOKEN: Telegram bot token from BotFather
# - WEBHOOK_SECRET: random secret used in webhook URL path
# - ADMIN_USER_IDS: comma-separated Telegram numeric user ids
# - DB_PATH: optional, path to SQLite file
#
# Railway notes:
# - Use a Railway volume and point DB_PATH to that mounted path
# - Example DB_PATH: /data/kayscafe.db
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
raw_admin_ids = os.environ.get("ADMIN_USER_IDS", "").replace(";", ",")
ADMIN_USER_IDS = {
    int(x.strip())
    for x in raw_admin_ids.split(",")
    if x.strip()
}
DB_PATH = os.environ.get("DB_PATH", "/data/kayscafe.db")
PAYNOW_IMAGE_URL = os.environ.get("PAYNOW_IMAGE_URL", "").strip()
PAYNOW_IMAGE_PATH = os.environ.get("PAYNOW_IMAGE_PATH", "").strip()
PAYNOW_CAPTION = os.environ.get(
    "PAYNOW_CAPTION",
    "Please complete payment via PayNow and send proof of payment.",
).strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()
PENDING_ADMIN_ACTIONS: dict[int, dict[str, Any]] = {}
telegram_loop = asyncio.new_event_loop()


# ============================================================
# DATABASE HELPERS
# ============================================================
def get_conn() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn



def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            available INTEGER NOT NULL DEFAULT 1,
            hidden INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS carts (
            user_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, item_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            items_json TEXT NOT NULL,
            total_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed'
        )
        """
    )

    conn.commit()
    seed_menu_items(conn)
    conn.close()


def _run_telegram_loop() -> None:
    asyncio.set_event_loop(telegram_loop)
    telegram_loop.run_forever()


def run_telegram_coroutine(coro):
    future = asyncio.run_coroutine_threadsafe(coro, telegram_loop)
    return future.result()



def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS



def cents_to_money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def parse_order_items(items_json: str) -> list[dict[str, Any]]:
    loaded = json.loads(items_json)
    return loaded if isinstance(loaded, list) else []



def seed_menu_items(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()

    items = [
        (
            "hojicha_latte",
            "Drinks",
            "Hojicha Latte",
            "Hojicha Latte Iced 280ml",
            550,
            1,
            0,
        ),
        (
            "matcha_latte",
            "Drinks",
            "Matcha Latte",
            "Matcha Latte Iced 280ml",
            600,
            1,
            0,
        ),
        (
            "banana_pudding_matcha_latte",
            "Drinks",
            "Banana Pudding Matcha Latte",
            "Matcha Latte topped with Banana Pudding and biscoff crumbs",
            690,
            0,
            0,
        ),
        (
            "banana_pudding_hojicha_latte",
            "Drinks",
            "Banana Pudding Hojicha Latte",
            "Hojicha Latte topped with banana pudding and biscoff crumbs",
            690,
            0,
            0,
        ),
        (
            "strawberry_matcha",
            "Drinks",
            "Strawberry Matcha",
            "Matcha Latte Iced with Strawberry Jam 280ml",
            650,
            1,
            0,
        ),
        (
            "strawberry_hojicha",
            "Drinks",
            "Strawberry Hojicha",
            "Hojicha Latte with Strawberry Puree",
            650,
            1,
            0,
        ),
        (
            "banana_pudding_90g",
            "Desserts",
            "Banana Pudding (90g)",
            "Only available on Friday, Saturday, Sunday while stocks last",
            400,
            0,
            0,
        ),
    ]

    conn.executemany(
        """
        INSERT OR IGNORE INTO menu_items
        (id, category, name, description, price_cents, available, hidden)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        items,
    )
    conn.commit()

    if owns_conn:
        conn.close()



def fetch_categories() -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category FROM menu_items WHERE hidden = 0 ORDER BY category"
    ).fetchall()
    conn.close()
    return [row["category"] for row in rows]



def fetch_items_by_category(category: str) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM menu_items WHERE category = ? AND hidden = 0 ORDER BY name",
        (category,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]



def fetch_item(item_id: str) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM menu_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None



def set_item_availability(item_id: str, available: bool) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE menu_items SET available = ? WHERE id = ?",
        (1 if available else 0, item_id),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated



def hide_item(item_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE menu_items SET hidden = 1 WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def unhide_item(item_id: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE menu_items SET hidden = 0 WHERE id = ?",
        (item_id,),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated



def slugify_item_id(name: str) -> str:
    cleaned = []
    for ch in name.lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in {" ", "-", "/"}:
            cleaned.append("_")
    item_id = "".join(cleaned).strip("_")
    while "__" in item_id:
        item_id = item_id.replace("__", "_")
    return item_id or "item"


def generate_unique_item_id(name: str) -> str:
    base = slugify_item_id(name)
    candidate = base
    counter = 2
    while fetch_item(candidate):
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def add_item(category: str, name: str, price_cents: int, description: str) -> tuple[bool, str, str | None]:
    item_id = generate_unique_item_id(name)
    conn = get_conn()
    conn.execute(
        """
        INSERT INTO menu_items (id, category, name, description, price_cents, available, hidden)
        VALUES (?, ?, ?, ?, ?, 1, 0)
        """,
        (item_id, category, name, description, price_cents),
    )
    conn.commit()
    conn.close()
    return True, "Item added successfully.", item_id



def edit_item(item_id: str, new_name: str | None = None, new_price_cents: int | None = None) -> tuple[bool, str]:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM menu_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if not row:
        conn.close()
        return False, "Item ID not found."

    updates = {}
    if new_name is not None and new_name.strip():
        updates["name"] = new_name.strip()
    if new_price_cents is not None:
        if new_price_cents < 0:
            conn.close()
            return False, "Price cannot be negative."
        updates["price_cents"] = new_price_cents

    if not updates:
        conn.close()
        return False, "No valid fields to update."

    set_clause = ", ".join(f"{key} = ?" for key in updates.keys())
    values = list(updates.values()) + [item_id]
    conn.execute(f"UPDATE menu_items SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()
    return True, "Item updated successfully."


def remove_item(item_id: str) -> tuple[bool, str]:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return False, "Item not found."

    conn.execute("DELETE FROM carts WHERE item_id = ?", (item_id,))
    conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    conn.commit()
    conn.close()
    return True, "Item removed successfully."



def list_all_items_for_admin() -> str:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM menu_items ORDER BY category, name"
    ).fetchall()
    conn.close()

    lines = ["*Menu Items*", "Tap buttons in /adminmenu to manage items.", ""]
    current_category = None
    for row in rows:
        item = dict(row)
        if item["category"] != current_category:
            current_category = item["category"]
            lines.append(f"*{current_category}*")
        status = "Available" if item["available"] else "Sold Out"
        if item.get("hidden"):
            status += ", Hidden"
        lines.append(
            f"- {item['name']} ({cents_to_money(item['price_cents'])}) [{status}]"
        )
    return "".join(lines)



def fetch_admin_items() -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM menu_items ORDER BY category, name"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]



def build_admin_menu_keyboard() -> InlineKeyboardMarkup:
    rows = []
    current_category = None
    for item in fetch_admin_items():
        if item["category"] != current_category:
            current_category = item["category"]
            rows.append([InlineKeyboardButton(f"— {current_category} —", callback_data="admin_noop")])
        status_emoji = "🟢" if item["available"] and not item.get("hidden") else "🔴"
        rows.append([
            InlineKeyboardButton(
                f"{status_emoji} {item['name']}",
                callback_data=f"admin_item:{item['id']}",
            )
        ])

    rows.append([InlineKeyboardButton("➕ Add New Item", callback_data="admin_add_start")])
    rows.append([InlineKeyboardButton("Refresh Admin Menu", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)



def build_admin_item_keyboard(item_id: str, hidden: bool = False) -> InlineKeyboardMarkup:
    hide_label = "Unhide Item" if hidden else "Hide Item"
    hide_callback = f"admin_unhide:{item_id}" if hidden else f"admin_hide:{item_id}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Mark Sold Out", callback_data=f"admin_soldout:{item_id}"),
            InlineKeyboardButton("Mark Available", callback_data=f"admin_available:{item_id}"),
        ],
        [
            InlineKeyboardButton(hide_label, callback_data=hide_callback),
            InlineKeyboardButton("Remove Item", callback_data=f"admin_remove_confirm:{item_id}"),
        ],
        [
            InlineKeyboardButton("Rename Item", callback_data=f"admin_rename_start:{item_id}"),
            InlineKeyboardButton("Change Price", callback_data=f"admin_price_start:{item_id}"),
        ],
        [InlineKeyboardButton("Back to Admin Menu", callback_data="admin_menu")],
    ])



def build_admin_item_text(item: dict[str, Any]) -> str:
    status = "Available" if item["available"] else "Sold Out"
    if item.get("hidden"):
        status += ", Hidden"

    return (
        "*Admin Item Panel*\n"
        f"*Name:* {item['name']}\n"
        f"*Category:* {item['category']}\n"
        f"*Price:* {cents_to_money(item['price_cents'])}\n"
        f"*Status:* {status}\n"
        f"*ID:* `{item['id']}`\n\n"
        "Choose an action below."
    )



def add_to_cart(user_id: int, item_id: str) -> None:
    item = fetch_item(item_id)
    if not item:
        raise ValueError("Item not found")

    conn = get_conn()
    existing = conn.execute(
        "SELECT quantity FROM carts WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchone()

    if existing:
        conn.execute(
            "UPDATE carts SET quantity = quantity + 1 WHERE user_id = ? AND item_id = ?",
            (user_id, item_id),
        )
    else:
        conn.execute(
            "INSERT INTO carts (user_id, item_id, quantity) VALUES (?, ?, 1)",
            (user_id, item_id),
        )

    conn.commit()
    conn.close()



def decrease_cart_item(user_id: int, item_id: str) -> None:
    conn = get_conn()
    existing = conn.execute(
        "SELECT quantity FROM carts WHERE user_id = ? AND item_id = ?",
        (user_id, item_id),
    ).fetchone()

    if existing:
        new_qty = int(existing["quantity"]) - 1
        if new_qty <= 0:
            conn.execute(
                "DELETE FROM carts WHERE user_id = ? AND item_id = ?",
                (user_id, item_id),
            )
        else:
            conn.execute(
                "UPDATE carts SET quantity = ? WHERE user_id = ? AND item_id = ?",
                (new_qty, user_id, item_id),
            )

    conn.commit()
    conn.close()



def clear_cart(user_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()



def fetch_cart(user_id: int) -> list[dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT c.item_id, c.quantity, m.name, m.price_cents, m.available
        FROM carts c
        JOIN menu_items m ON c.item_id = m.id
        WHERE c.user_id = ?
        ORDER BY m.name
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]



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



def build_receipt(customer_name: str, order_id: int, items: list[dict[str, Any]], total_cents: int) -> str:
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


async def send_paynow_photo(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if PAYNOW_IMAGE_URL:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=PAYNOW_IMAGE_URL,
            caption=PAYNOW_CAPTION,
        )
        return

    if PAYNOW_IMAGE_PATH:
        with open(PAYNOW_IMAGE_PATH, "rb") as image_file:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=image_file,
                caption=PAYNOW_CAPTION,
            )


def create_order(user_id: int, customer_name: str) -> tuple[int, str]:
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

    conn = get_conn()
    cur = conn.execute(
        """
        INSERT INTO orders (user_id, customer_name, items_json, total_cents, created_at, status)
        VALUES (?, ?, ?, ?, ?, 'awaiting_payment')
        """,
        (
            user_id,
            customer_name,
            json.dumps(items),
            total_cents,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    order_id = cur.lastrowid
    conn.commit()
    conn.close()

    clear_cart(user_id)
    return order_id, build_receipt(customer_name, order_id, items, total_cents)


def fetch_order(order_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM orders WHERE id = ?",
        (order_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def fetch_latest_unpaid_order(user_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    row = conn.execute(
        """
        SELECT * FROM orders
        WHERE user_id = ? AND status IN ('awaiting_payment', 'payment_submitted')
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_order_status(order_id: int, status: str) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "UPDATE orders SET status = ? WHERE id = ?",
        (status, order_id),
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def build_payment_pending_text(order_id: int, total_cents: int) -> str:
    return (
        "Order created. Payment is pending.\n\n"
        f"Order ID: `{order_id}`\n"
        f"Total: {cents_to_money(total_cents)}\n\n"
        "Please complete payment via PayNow, then send your payment screenshot here."
    )


def _escape_md(text: str) -> str:
    """Escape special characters for Telegram legacy Markdown."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def build_admin_payment_review_text(order: dict[str, Any]) -> str:
    return (
        "*Payment Proof Received*\n"
        f"Order ID: `{order['id']}`\n"
        f"Customer: {_escape_md(order['customer_name'])}\n"
        f"Telegram User ID: `{order['user_id']}`\n"
        f"Total: {cents_to_money(order['total_cents'])}\n"
        f"Status: {_escape_md(order['status'])}\n\n"
        "Review the screenshot above and mark the order as paid when verified."
    )


def build_receipt_from_order(order: dict[str, Any]) -> str:
    return build_receipt(
        order["customer_name"],
        order["id"],
        parse_order_items(order["items_json"]),
        order["total_cents"],
    )


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


def build_payment_review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Mark Paid", callback_data=f"admin_mark_paid:{order_id}"),
            InlineKeyboardButton("Reject", callback_data=f"admin_reject_payment:{order_id}"),
        ],
    ])


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

    await update.message.reply_text(
        list_all_items_for_admin(),
        parse_mode="Markdown",
        reply_markup=build_admin_menu_keyboard(),
    )


async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    pending = PENDING_ADMIN_ACTIONS.get(user_id)
    if not pending or not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if text == "/cancel":
        PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await update.message.reply_text("Admin action cancelled.")
        return

    action = pending.get("action")

    if action == "rename":
        item_id = pending["item_id"]
        ok, message = edit_item(item_id, new_name=text)
        PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await update.message.reply_text(message)
        return

    if action == "price":
        item_id = pending["item_id"]
        try:
            dollars = float(text)
            new_price_cents = int(round(dollars * 100))
        except ValueError:
            await update.message.reply_text("Please enter a valid price like 6.50")
            return
        ok, message = edit_item(item_id, new_price_cents=new_price_cents)
        PENDING_ADMIN_ACTIONS.pop(user_id, None)
        await update.message.reply_text(message)
        return

    if action == "add_name":
        pending["name"] = text
        pending["action"] = "add_category"
        await update.message.reply_text("Send the category for this new item, for example Drinks or Desserts.")
        return

    if action == "add_category":
        pending["category"] = text
        pending["action"] = "add_price"
        await update.message.reply_text("Send the price, for example 6.50")
        return

    if action == "add_price":
        try:
            dollars = float(text)
            pending["price_cents"] = int(round(dollars * 100))
        except ValueError:
            await update.message.reply_text("Please enter a valid price like 6.50")
            return
        pending["action"] = "add_description"
        await update.message.reply_text("Send the item description.")
        return

    if action == "add_description":
        ok, message, item_id = add_item(
            pending["category"],
            pending["name"],
            pending["price_cents"],
            text,
        )
        PENDING_ADMIN_ACTIONS.pop(user_id, None)
        if ok:
            await update.message.reply_text(f"{message} Created item: {pending['name']}")
        else:
            await update.message.reply_text(message)
        return





async def handle_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.photo:
        return

    user_id = update.effective_user.id

    order = fetch_latest_unpaid_order(user_id)
    if not order:
        await update.message.reply_text(
            "I could not find a pending order for this payment screenshot."
        )
        return

    update_order_status(order["id"], "payment_submitted")
    order = fetch_order(order["id"])
    photo = update.message.photo[-1].file_id

    notified = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=photo,
                caption=build_admin_payment_review_text(order),
                parse_mode="Markdown",
                reply_markup=build_payment_review_keyboard(order["id"]),
            )
            notified = True
        except Exception as exc:
            logger.warning("Failed to forward payment proof to admin %s: %s", admin_id, exc)

    if not notified:
        await update.message.reply_text(
            "Warning: could not reach any admin to review your payment. Please contact us directly."
        )
    else:
        await update.message.reply_text(
            "Payment screenshot received. Your order will be confirmed after an admin verifies it."
        )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    customer_name = query.from_user.full_name or "Customer"
    data = query.data

    if data == "admin_noop":
        return

    if data == "admin_menu":
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        await query.edit_message_text(
            list_all_items_for_admin(),
            parse_mode="Markdown",
            reply_markup=build_admin_menu_keyboard(),
        )
        return

    if data.startswith("admin_mark_paid:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        order_id = int(data.split(":", 1)[1])
        order = fetch_order(order_id)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return
        if order["status"] == "paid":
            await query.answer("Order is already marked as paid.", show_alert=True)
            return

        update_order_status(order_id, "paid")
        order = fetch_order(order_id)
        receipt = build_receipt_from_order(order)

        await query.edit_message_caption(
            caption=build_admin_payment_review_text(order),
            parse_mode="Markdown",
            reply_markup=None,
        )
        await query.answer("Order marked as paid.")

        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=receipt,
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.warning("Failed to send final confirmation for order %s: %s", order_id, exc)
        return

    if data.startswith("admin_reject_payment:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        order_id = int(data.split(":", 1)[1])
        order = fetch_order(order_id)
        if not order:
            await query.answer("Order not found.", show_alert=True)
            return
        if order["status"] == "paid":
            await query.answer("Order is already marked as paid.", show_alert=True)
            return

        update_order_status(order_id, "awaiting_payment")
        order = fetch_order(order_id)

        await query.edit_message_caption(
            caption=build_admin_payment_review_text(order),
            parse_mode="Markdown",
            reply_markup=None,
        )
        await query.answer("Payment rejected.")

        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=(
                    "Your payment screenshot could not be verified for Order #"
                    f"{order_id}. Please send a clear screenshot of your PayNow transfer, "
                    "or contact us for help."
                ),
            )
        except Exception as exc:
            logger.warning("Failed to notify customer of rejection for order %s: %s", order_id, exc)
        return

    if data.startswith("admin_item:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        await query.edit_message_text(
            build_admin_item_text(item),
            parse_mode="Markdown",
            reply_markup=build_admin_item_keyboard(item_id, bool(item.get("hidden"))),
        )
        return

    if data.startswith("admin_soldout:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        ok = set_item_availability(item_id, False)
        if not ok:
            await query.answer("Item not found.", show_alert=True)
            return
        item = fetch_item(item_id)
        await query.edit_message_text(
            build_admin_item_text(item),
            parse_mode="Markdown",
            reply_markup=build_admin_item_keyboard(item_id, bool(item.get("hidden"))),
        )
        await query.answer("Item marked as sold out.")
        return

    if data.startswith("admin_available:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        ok = set_item_availability(item_id, True)
        if not ok:
            await query.answer("Item not found.", show_alert=True)
            return
        item = fetch_item(item_id)
        await query.edit_message_text(
            build_admin_item_text(item),
            parse_mode="Markdown",
            reply_markup=build_admin_item_keyboard(item_id, bool(item.get("hidden"))),
        )
        await query.answer("Item marked as available.")
        return

    if data.startswith("admin_hide:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        ok = hide_item(item_id)
        if not ok:
            await query.answer("Item not found.", show_alert=True)
            return
        item = fetch_item(item_id)
        await query.edit_message_text(
            build_admin_item_text(item),
            parse_mode="Markdown",
            reply_markup=build_admin_item_keyboard(item_id, True),
        )
        await query.answer("Item hidden from customers.")
        return

    if data.startswith("admin_unhide:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        ok = unhide_item(item_id)
        if not ok:
            await query.answer("Item not found.", show_alert=True)
            return
        item = fetch_item(item_id)
        await query.edit_message_text(
            build_admin_item_text(item),
            parse_mode="Markdown",
            reply_markup=build_admin_item_keyboard(item_id, False),
        )
        await query.answer("Item is visible to customers again.")
        return

    if data == "admin_add_start":
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        PENDING_ADMIN_ACTIONS[user_id] = {"action": "add_name"}
        await query.message.reply_text("Send the name of the new item.")
        await query.answer("Add item started.")
        return

    if data.startswith("admin_rename_start:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        PENDING_ADMIN_ACTIONS[user_id] = {"action": "rename", "item_id": item_id}
        await query.message.reply_text(f"Send the new name for {item['name']}.")
        await query.answer("Rename started.")
        return

    if data.startswith("admin_price_start:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        PENDING_ADMIN_ACTIONS[user_id] = {"action": "price", "item_id": item_id}
        await query.message.reply_text(
            f"Send the new price for {item['name']}. Example: 6.50"
        )
        await query.answer("Price change started.")
        return

    if data.startswith("admin_remove_confirm:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes, Remove", callback_data=f"admin_remove_yes:{item_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"admin_item:{item_id}"),
            ]
        ])
        await query.edit_message_text(
            f"Remove *{item['name']}* permanently?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    if data.startswith("admin_remove_yes:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        ok, message = remove_item(item_id)
        await query.answer(message)
        await query.edit_message_text(
            list_all_items_for_admin(),
            parse_mode="Markdown",
            reply_markup=build_admin_menu_keyboard(),
        )
        return

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
            order_id, _receipt = create_order(user_id, customer_name)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        order = fetch_order(order_id)
        await query.edit_message_text(
            build_payment_pending_text(order_id, order["total_cents"]),
            parse_mode="Markdown",
        )
        try:
            await send_paynow_photo(query.message.chat_id, context)
        except Exception as exc:
            logger.warning("Failed to send PayNow image to user %s: %s", user_id, exc)

        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        "*New Order Awaiting Payment*\n\n"
                        f"Order ID: `{order_id}`\n"
                        f"Customer: {customer_name}\n"
                        f"Telegram User ID: `{user_id}`\n"
                        f"Total: {cents_to_money(order['total_cents'])}"
                    ),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                logger.warning("Failed to notify admin %s: %s", admin_id, exc)
        return

    if data == "noop":
        return


# ============================================================
# FLASK ROUTES FOR RAILWAY
# ============================================================
@flask_app.get("/")
def healthcheck():
    return jsonify({"ok": True, "message": "Telegram cafe bot is running."})


@flask_app.post(f"/webhook/{WEBHOOK_SECRET}")
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)
        run_telegram_coroutine(telegram_app.process_update(update))
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
from telegram.ext import MessageHandler, filters

telegram_app.add_handler(CallbackQueryHandler(button_handler))
telegram_app.add_handler(CommandHandler("cancel", handle_admin_text))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text))
telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_payment_screenshot))

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is missing. Set it before deploying.")

init_db()
threading.Thread(target=_run_telegram_loop, daemon=True).start()
run_telegram_coroutine(telegram_app.initialize())



if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    flask_app.run(host="0.0.0.0", port=port, debug=True)


# ============================================================
# requirements.txt
# ============================================================
# python-telegram-bot==22.5
# Flask==3.0.3
# gunicorn==22.0.0
#
# ============================================================
# Dockerfile
# ============================================================
# FROM python:3.11-slim
# WORKDIR /app
# COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
# COPY . .
# CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 0 kayscafe:flask_app


# ============================================================
# RAILWAY SETUP NOTES
# ============================================================
# 1. Add a Railway volume and mount it at /data
# 2. Set variables:
#    BOT_TOKEN=<your token>
#    WEBHOOK_SECRET=<your secret>
#    ADMIN_USER_IDS=1403094785,547731983
#    DB_PATH=/data/kayscafe.db
# 3. Admin uses /adminmenu and buttons instead of /edititem or /additem
# 3. Generate a Railway domain
# 4. Set Telegram webhook to:
#    https://YOUR-RAILWAY-DOMAIN/webhook/YOUR_SECRET
