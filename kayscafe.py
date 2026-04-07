import os
import json
import sqlite3
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

# ============================================================
# RAILWAY TELEGRAM BOT FOR HOME CAFE (SQLITE VERSION)
# ============================================================
# Features:
# - Webhook mode for Railway
# - Customer ordering flow
# - Receipt-style order confirmation
# - Admin-only commands to mark items sold out / available
# - Admin-only commands to add, hide, and edit items
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)
telegram_app = Application.builder().token(BOT_TOKEN).build()


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



def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS



def cents_to_money(cents: int) -> str:
    return f"${cents / 100:.2f}"



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



def add_item(item_id: str, category: str, name: str, price_cents: int, description: str) -> tuple[bool, str]:
    conn = get_conn()
    existing = conn.execute(
        "SELECT 1 FROM menu_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if existing:
        conn.close()
        return False, "Item ID already exists."

    conn.execute(
        """
        INSERT INTO menu_items (id, category, name, description, price_cents, available, hidden)
        VALUES (?, ?, ?, ?, ?, 1, 0)
        """,
        (item_id, category, name, description, price_cents),
    )
    conn.commit()
    conn.close()
    return True, "Item added successfully."



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

    rows.append([InlineKeyboardButton("Refresh Admin Menu", callback_data="admin_menu")])
    return InlineKeyboardMarkup(rows)



def build_admin_item_keyboard(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Mark Sold Out", callback_data=f"admin_soldout:{item_id}"),
            InlineKeyboardButton("Mark Available", callback_data=f"admin_available:{item_id}"),
        ],
        [
            InlineKeyboardButton("Hide Item", callback_data=f"admin_hide:{item_id}"),
        ],
        [
            InlineKeyboardButton("Rename Help", callback_data=f"admin_rename_help:{item_id}"),
            InlineKeyboardButton("Price Help", callback_data=f"admin_price_help:{item_id}"),
        ],
        [InlineKeyboardButton("Back to Admin Menu", callback_data="admin_menu")],
    ])



def build_admin_item_text(item: dict[str, Any]) -> str:
    status = "Available" if item["available"] else "Sold Out"
    if item.get("hidden"):
        status += ", Hidden"

    return (
        "*Admin Item Panel*"
        f"*Name:* {item['name']}"
        f"*Category:* {item['category']}"
        f"*Price:* {cents_to_money(item['price_cents'])}"
        f"*Status:* {status}"
        f"*ID:* `{item['id']}`"
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
        VALUES (?, ?, ?, ?, ?, 'confirmed')
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

    await update.message.reply_text(
        list_all_items_for_admin(),
        parse_mode="Markdown",
        reply_markup=build_admin_menu_keyboard(),
    )


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
            "Usage: /edititem <item_id> | <new_name> | <new_price_cents>\n"
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
            reply_markup=build_admin_item_keyboard(item_id),
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
            reply_markup=build_admin_item_keyboard(item_id),
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
            reply_markup=build_admin_item_keyboard(item_id),
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
            reply_markup=build_admin_item_keyboard(item_id),
        )
        await query.answer("Item hidden from customers.")
        return

    if data.startswith("admin_rename_help:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        await query.message.reply_text(
            f"Rename this item with: /edititem {item_id} | New Item Name | Current name: {item['name']}"
        )
        await query.answer("Rename instructions sent.")
        return

    if data.startswith("admin_price_help:"):
        if not is_admin(user_id):
            await query.answer("You are not allowed to use this action.", show_alert=True)
            return
        item_id = data.split(":", 1)[1]
        item = fetch_item(item_id)
        if not item:
            await query.answer("Item not found.", show_alert=True)
            return
        await query.message.reply_text(
            f"Change price with: /edititem {item_id} | | 650 Current price: {cents_to_money(item['price_cents'])} Use cents, so 650 means $6.50."
        )
        await query.answer("Price instructions sent.")
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
            order_id, receipt = create_order(user_id, customer_name)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        await query.edit_message_text(receipt, parse_mode="Markdown")

        for admin_id in ADMIN_USER_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"📥 *New Order Received*\n\n{receipt}",
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

init_db()

import asyncio
asyncio.run(telegram_app.initialize())


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
# 3. Generate a Railway domain
# 4. Set Telegram webhook to:
#    https://YOUR-RAILWAY-DOMAIN/webhook/YOUR_SECRET
