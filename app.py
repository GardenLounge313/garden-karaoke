#!/usr/bin/env python3
"""
The Garden Lounge - Karaoke Room Reservation System
With Stripe payment processing ($40 / hour).
"""

import os
import sqlite3
import json
from datetime import datetime, date, time, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify, g,
    redirect, url_for, session, flash, abort
)

# Stripe (optional until keys are set)
try:
    import stripe
    STRIPE_AVAILABLE = True
except ImportError:
    STRIPE_AVAILABLE = False
    stripe = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "garden-karaoke-super-secret-change-me-in-prod-2026")

# Simple admin password (change this!)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "garden2026")

# Stripe keys (get from https://dashboard.stripe.com/apikeys)
# Use test keys (sk_test_... / pk_test_...) while developing
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")  # optional for now

if STRIPE_AVAILABLE and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

DB_PATH = "/tmp/bookings.db"

# Pricing
PRICE_PER_HOUR_CENTS = 4000          # $40.00 per hour
CURRENCY = "usd"

# Slot settings
SLOT_INTERVAL_MINUTES = 30
MIN_DURATION_MINUTES = 60
MAX_DURATION_MINUTES = 180
DURATION_OPTIONS = [60, 90, 120, 150, 180]
MAX_DAYS_AHEAD = 60
PARTY_SIZE_MAX = 12

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_date TEXT NOT NULL,
            start_minutes INTEGER NOT NULL,
            end_minutes INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT,
            party_size INTEGER DEFAULT 1,
            notes TEXT,
            status TEXT DEFAULT 'confirmed',
            amount_cents INTEGER,
            stripe_session_id TEXT,
            stripe_payment_intent TEXT,
            payment_status TEXT DEFAULT 'paid',
            created_at TEXT NOT NULL,
            cancelled_at TEXT
        )
    """)
    # Safe migration for existing DBs
    try:
        db.execute("ALTER TABLE bookings ADD COLUMN amount_cents INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE bookings ADD COLUMN stripe_session_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE bookings ADD COLUMN stripe_payment_intent TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        db.execute("ALTER TABLE bookings ADD COLUMN payment_status TEXT DEFAULT 'paid'")
    except sqlite3.OperationalError:
        pass

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_bookings_date_status
        ON bookings (booking_date, status)
    """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_stripe_session
        ON bookings (stripe_session_id)
        WHERE stripe_session_id IS NOT NULL
    """)
    db.commit()
    db.close()
    print(f"Database initialized at {DB_PATH}")

# ---------------------------------------------------------------------------
# Business logic: hours & availability
# ---------------------------------------------------------------------------
def get_hours_for_date(d: date):
    wd = d.weekday()  # Mon=0 ... Sun=6
    open_m = 16 * 60  # 4:00 PM
    if wd in (3, 4, 5):  # Thu, Fri, Sat
        close_m = 24 * 60 + 2 * 60  # 2:00 AM next day
    else:
        close_m = 24 * 60  # midnight
    return open_m, close_m

def minutes_to_str(m: int) -> str:
    total = m % (24 * 60)
    h = total // 60
    mi = total % 60
    period = "AM" if h < 12 else "PM"
    h12 = h % 12
    if h12 == 0:
        h12 = 12
    return f"{h12}:{mi:02d} {period}"

def get_existing_bookings(db, booking_date: str):
    rows = db.execute(
        """
        SELECT start_minutes, end_minutes
        FROM bookings
        WHERE booking_date = ? AND status = 'confirmed'
        ORDER BY start_minutes
        """,
        (booking_date,),
    ).fetchall()
    return [(r["start_minutes"], r["end_minutes"]) for r in rows]

def has_overlap(start: int, end: int, existing: list) -> bool:
    for s, e in existing:
        if start < e and end > s:
            return True
    return False

def generate_available_slots(d: date, existing: list):
    open_m, close_m = get_hours_for_date(d)
    today = date.today()
    now = datetime.now()

    min_start = open_m
    if d == today:
        # Show remaining slots from now (no big buffer)
        current_m = now.hour * 60 + now.minute
        min_start = max(open_m, current_m)

    if d < today:
        return []

    available = []
    start = min_start
    if start % SLOT_INTERVAL_MINUTES != 0:
        start = ((start // SLOT_INTERVAL_MINUTES) + 1) * SLOT_INTERVAL_MINUTES

    while start + MIN_DURATION_MINUTES <= close_m:
        free_durations = []
        for dur in DURATION_OPTIONS:
            end = start + dur
            if end > close_m:
                break
            if not has_overlap(start, end, existing):
                free_durations.append(dur)
        if free_durations:
            available.append({
                "start_minutes": start,
                "start_display": minutes_to_str(start),
                "available_durations": free_durations,
            })
        start += SLOT_INTERVAL_MINUTES
    return available

def calc_amount_cents(duration_minutes: int) -> int:
    """$40 per hour, pro-rated exactly (e.g. 90 min = $60)."""
    hours = duration_minutes / 60.0
    return int(round(PRICE_PER_HOUR_CENTS * hours))

def format_money(cents: int) -> str:
    return f"${cents / 100:.2f}"

# ---------------------------------------------------------------------------
# Auth helper for admin
# ---------------------------------------------------------------------------
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Routes - Public
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        stripe_publishable_key=STRIPE_PUBLISHABLE_KEY,
        price_per_hour=PRICE_PER_HOUR_CENTS // 100,
    )

@app.route("/api/hours")
def api_hours():
    return jsonify({
        "sun_wed": "4:00 PM – 12:00 AM (midnight)",
        "thu_sat": "4:00 PM – 2:00 AM",
        "slot_interval": SLOT_INTERVAL_MINUTES,
        "durations": DURATION_OPTIONS,
        "max_days_ahead": MAX_DAYS_AHEAD,
        "party_max": PARTY_SIZE_MAX,
        "price_per_hour_cents": PRICE_PER_HOUR_CENTS,
        "price_per_hour": PRICE_PER_HOUR_CENTS // 100,
        "currency": CURRENCY,
        "stripe_enabled": bool(STRIPE_SECRET_KEY and STRIPE_AVAILABLE),
    })

@app.route("/api/availability")
def api_availability():
    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date required (YYYY-MM-DD)"}), 400
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "invalid date format"}), 400

    today = date.today()
    if d < today or d > today + timedelta(days=MAX_DAYS_AHEAD):
        return jsonify({"error": "date out of range", "slots": []}), 400

    db = get_db()
    existing = get_existing_bookings(db, date_str)
    slots = generate_available_slots(d, existing)

    open_m, close_m = get_hours_for_date(d)
    return jsonify({
        "date": date_str,
        "open": minutes_to_str(open_m),
        "close": minutes_to_str(close_m),
        "existing_count": len(existing),
        "slots": slots,
        "price_per_hour_cents": PRICE_PER_HOUR_CENTS,
    })

@app.route("/api/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """Create a Stripe Checkout Session. Booking is only written after successful payment."""
    if not (STRIPE_AVAILABLE and STRIPE_SECRET_KEY):
        return jsonify({
            "error": "Stripe is not configured. Set STRIPE_SECRET_KEY and STRIPE_PUBLISHABLE_KEY environment variables."
        }), 503

    data = request.get_json(force=True, silent=True) or {}
    required = ["date", "start_minutes", "duration_minutes", "name", "phone"]
    for r in required:
        if r not in data or data[r] in (None, ""):
            return jsonify({"error": f"Missing required field: {r}"}), 400

    try:
        d = datetime.strptime(data["date"], "%Y-%m-%d").date()
        start_m = int(data["start_minutes"])
        dur = int(data["duration_minutes"])
        name = str(data["name"]).strip()[:100]
        phone = str(data["phone"]).strip()[:30]
        email = str(data.get("email") or "").strip()[:120]
        party = int(data.get("party_size") or 1)
        notes = str(data.get("notes") or "").strip()[:500]
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid data: {e}"}), 400

    if not name or not phone:
        return jsonify({"error": "Name and phone are required"}), 400
    if dur not in DURATION_OPTIONS:
        return jsonify({"error": "Invalid duration"}), 400
    if party < 1 or party > PARTY_SIZE_MAX:
        return jsonify({"error": f"Party size must be 1–{PARTY_SIZE_MAX}"}), 400

    today = date.today()
    if d < today or d > today + timedelta(days=MAX_DAYS_AHEAD):
        return jsonify({"error": "Date out of allowed range"}), 400

    end_m = start_m + dur
    open_m, close_m = get_hours_for_date(d)
    if start_m < open_m or end_m > close_m:
        return jsonify({"error": "Selected time is outside open hours"}), 400

    # Availability check (prevents taking payment for already-booked slots)
    db = get_db()
    existing = get_existing_bookings(db, data["date"])
    if has_overlap(start_m, end_m, existing):
        return jsonify({"error": "Sorry, that time slot was just booked. Please pick another."}), 409

    min_start = open_m
    # Temporarily show all times today for testing
    # if d == today:
    #     current_m = now.hour * 60 + now.minute
    #     min_start = max(open_m, current_m)

    amount_cents = calc_amount_cents(dur)
    start_display = minutes_to_str(start_m)
    end_display = minutes_to_str(end_m)
    hours_label = f"{dur // 60}h" if dur % 60 == 0 else f"{dur // 60}h {dur % 60}m"
    if dur == 60:
        hours_label = "1 hour"
    elif dur == 90:
        hours_label = "1.5 hours"
    elif dur == 120:
        hours_label = "2 hours"
    elif dur == 150:
        hours_label = "2.5 hours"
    elif dur == 180:
        hours_label = "3 hours"

    # Build success / cancel URLs
    # Use request host so it works on any domain
    base = request.host_url.rstrip("/")
    success_url = f"{base}/booking/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/?canceled=1"

    try:
        session_obj = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": CURRENCY,
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": "Karaoke Room – The Garden Lounge",
                        "description": (
                            f"{data['date']} · {start_display} – {end_display} ({hours_label})\n"
                            f"Guest: {name} · Party of {party}"
                        ),
                        "images": [],  # optional: add a logo URL later
                    },
                },
                "quantity": 1,
            }],
            customer_email=email if email else None,
            metadata={
                "booking_date": data["date"],
                "start_minutes": str(start_m),
                "end_minutes": str(end_m),
                "duration_minutes": str(dur),
                "name": name,
                "phone": phone,
                "email": email or "",
                "party_size": str(party),
                "notes": notes or "",
                "amount_cents": str(amount_cents),
            },
            success_url=success_url,
            cancel_url=cancel_url,
            expires_at=int((datetime.utcnow() + timedelta(minutes=30)).timestamp()),
        )
    except Exception as e:
        app.logger.exception("Stripe session creation failed")
        return jsonify({"error": f"Payment setup failed: {str(e)}"}), 500

    return jsonify({
        "checkout_url": session_obj.url,
        "session_id": session_obj.id,
        "amount_cents": amount_cents,
        "amount_display": format_money(amount_cents),
    })


@app.route("/booking/success")
def booking_success():
    """After Stripe payment, verify and create the booking."""
    session_id = request.args.get("session_id")
    if not session_id:
        flash("Missing payment session.", "error")
        return redirect(url_for("index"))

    if not (STRIPE_AVAILABLE and STRIPE_SECRET_KEY):
        flash("Stripe not configured.", "error")
        return redirect(url_for("index"))

    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        flash("Could not verify payment. Please contact the bar if you were charged.", "error")
        return redirect(url_for("index"))

    if checkout_session.payment_status != "paid":
        flash("Payment was not completed.", "error")
        return redirect(url_for("index"))

    # Idempotency: already processed?
    db = get_db()
    existing = db.execute(
        "SELECT id FROM bookings WHERE stripe_session_id = ?", (session_id,)
    ).fetchone()
    if existing:
        # Already booked — just show confirmation again
        booking = db.execute("SELECT * FROM bookings WHERE id = ?", (existing["id"],)).fetchone()
        return render_template(
            "success.html",
            booking=booking,
            minutes_to_str=minutes_to_str,
            format_money=format_money,
        )

    meta = checkout_session.metadata or {}
    try:
        booking_date = meta["booking_date"]
        start_m = int(meta["start_minutes"])
        end_m = int(meta["end_minutes"])
        name = meta["name"]
        phone = meta["phone"]
        email = meta.get("email") or None
        party = int(meta.get("party_size") or 1)
        notes = meta.get("notes") or None
        amount_cents = int(meta.get("amount_cents") or checkout_session.amount_total or 0)
    except (KeyError, ValueError):
        flash("Invalid booking data from payment. Please contact the bar.", "error")
        return redirect(url_for("index"))

    # Final availability check (someone else may have snuck in)
    existing_slots = get_existing_bookings(db, booking_date)
    if has_overlap(start_m, end_m, existing_slots):
        # Rare race: refund? For now show message and let staff handle
        flash(
            "Payment received, but that exact time was just taken. "
            "Please call the bar — we will either rebook you or refund immediately.",
            "error",
        )
        # Still record a note for staff? Skip for simplicity.
        return redirect(url_for("index"))

    created = datetime.now().isoformat(timespec="seconds")
    payment_intent = checkout_session.payment_intent
    if isinstance(payment_intent, str):
        pi_id = payment_intent
    else:
        pi_id = getattr(payment_intent, "id", None)

    cur = db.execute(
        """
        INSERT INTO bookings
        (booking_date, start_minutes, end_minutes, name, phone, email,
         party_size, notes, status, amount_cents, stripe_session_id,
         stripe_payment_intent, payment_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, 'paid', ?)
        """,
        (
            booking_date, start_m, end_m, name, phone, email,
            party, notes, amount_cents, session_id, pi_id, created,
        ),
    )
    db.commit()
    booking_id = cur.lastrowid

    booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    return render_template(
        "success.html",
        booking=booking,
        minutes_to_str=minutes_to_str,
        format_money=format_money,
    )


# Keep old /api/book for fallback / testing without Stripe (disabled if Stripe is live)
@app.route("/api/book", methods=["POST"])
def api_book():
    """Direct book endpoint — only allowed when Stripe is NOT configured (dev mode)."""
    if STRIPE_SECRET_KEY and STRIPE_AVAILABLE:
        return jsonify({
            "error": "Direct booking disabled. Please use the payment flow."
        }), 403

    # ... (original logic kept for offline testing)
    data = request.get_json(force=True, silent=True) or {}
    required = ["date", "start_minutes", "duration_minutes", "name", "phone"]
    for r in required:
        if r not in data or data[r] in (None, ""):
            return jsonify({"error": f"Missing required field: {r}"}), 400

    try:
        d = datetime.strptime(data["date"], "%Y-%m-%d").date()
        start_m = int(data["start_minutes"])
        dur = int(data["duration_minutes"])
        name = str(data["name"]).strip()[:100]
        phone = str(data["phone"]).strip()[:30]
        email = str(data.get("email") or "").strip()[:120]
        party = int(data.get("party_size") or 1)
        notes = str(data.get("notes") or "").strip()[:500]
    except (ValueError, TypeError) as e:
        return jsonify({"error": f"Invalid data: {e}"}), 400

    if dur not in DURATION_OPTIONS or party < 1 or party > PARTY_SIZE_MAX:
        return jsonify({"error": "Invalid duration or party size"}), 400

    end_m = start_m + dur
    open_m, close_m = get_hours_for_date(d)
    if start_m < open_m or end_m > close_m:
        return jsonify({"error": "Outside open hours"}), 400

    db = get_db()
    existing = get_existing_bookings(db, data["date"])
    if has_overlap(start_m, end_m, existing):
        return jsonify({"error": "Slot no longer available"}), 409

    amount_cents = calc_amount_cents(dur)
    created = datetime.now().isoformat(timespec="seconds")
    cur = db.execute(
        """
        INSERT INTO bookings
        (booking_date, start_minutes, end_minutes, name, phone, email,
         party_size, notes, status, amount_cents, payment_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, 'dev-mode', ?)
        """,
        (data["date"], start_m, end_m, name, phone, email or None,
         party, notes or None, amount_cents, created),
    )
    db.commit()
    return jsonify({
        "success": True,
        "booking_id": cur.lastrowid,
        "message": "Booked (dev mode – no payment)",
        "summary": {
            "date": data["date"],
            "start": minutes_to_str(start_m),
            "end": minutes_to_str(end_m),
            "duration_minutes": dur,
            "name": name,
            "party_size": party,
            "amount": format_money(amount_cents),
        },
    })


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Incorrect password", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    today_str = date.today().isoformat()
    upcoming = db.execute(
        """
        SELECT * FROM bookings
        WHERE booking_date >= ? AND status = 'confirmed'
        ORDER BY booking_date, start_minutes
        """,
        (today_str,),
    ).fetchall()

    past = db.execute(
        """
        SELECT * FROM bookings
        WHERE booking_date < ? OR status = 'cancelled'
        ORDER BY booking_date DESC, start_minutes DESC
        LIMIT 50
        """,
        (today_str,),
    ).fetchall()

    return render_template(
        "admin.html",
        upcoming=upcoming,
        past=past,
        minutes_to_str=minutes_to_str,
        format_money=format_money,
    )

@app.route("/admin/cancel/<int:booking_id>", methods=["POST"])
@admin_required
def admin_cancel(booking_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM bookings WHERE id = ? AND status = 'confirmed'", (booking_id,)
    ).fetchone()
    if not row:
        flash("Booking not found or already cancelled.", "error")
        return redirect(url_for("admin_dashboard"))

    # Optional: you can add automatic Stripe refund here later using row["stripe_payment_intent"]
    db.execute(
        """
        UPDATE bookings
        SET status = 'cancelled', cancelled_at = ?
        WHERE id = ?
        """,
        (datetime.now().isoformat(timespec="seconds"), booking_id),
    )
    db.commit()
    flash(f"Booking #{booking_id} cancelled. (Remember to refund in Stripe Dashboard if needed.)", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/api/bookings")
@admin_required
def admin_api_bookings():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM bookings ORDER BY booking_date DESC, start_minutes LIMIT 200"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
# Force DB init on every startup for Render
with app.app_context():
    init_db()
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"

    stripe_status = "ENABLED" if (STRIPE_SECRET_KEY and STRIPE_AVAILABLE) else "NOT CONFIGURED (set STRIPE_SECRET_KEY)"
    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║   The Garden Lounge – Karaoke Room Reservation System            ║
║   Pricing: $40 / hour  ·  Stripe: {stripe_status:<30} ║
║                                                                  ║
║   Public booking : http://0.0.0.0:{port}/                           ║
║   Admin panel    : http://0.0.0.0:{port}/admin                      ║
║   Admin password : {ADMIN_PASSWORD}                                  ║
║                                                                  ║
║   Required env vars for payments:                                ║
║     STRIPE_SECRET_KEY=sk_live_... or sk_test_...                 ║
║     STRIPE_PUBLISHABLE_KEY=pk_live_... or pk_test_...            ║
║     ADMIN_PASSWORD=...   SECRET_KEY=...                          ║
╚══════════════════════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=port, debug=debug)
