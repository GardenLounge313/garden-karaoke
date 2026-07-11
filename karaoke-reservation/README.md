# The Garden Lounge – Karaoke Room Reservation System

Private karaoke room bookings with **$40 / hour** pricing and **Stripe** payment processing.

Guests pick a date & time → fill in their details → pay securely on Stripe → the slot is locked the moment payment succeeds.

## Features

- **$40 per hour** (exactly pro-rated: 1.5 h = $60, 2 h = $80, etc.)
- Real-time availability — booked slots disappear immediately after successful payment
- Correct open hours:
  - Sun–Wed: 4 PM – midnight
  - Thu–Sat: 4 PM – 2 AM
- Stripe Checkout (hosted, PCI-compliant, beautiful mobile experience)
- Admin dashboard with payment amounts visible
- SQLite — zero external database needed
- Mobile-first dark green “Garden” branding

## Quick Start (local)

```bash
cd karaoke-reservation
pip install -r requirements.txt

# Required for real payments (use Stripe test keys while developing)
export STRIPE_SECRET_KEY="sk_test_..."
export STRIPE_PUBLISHABLE_KEY="pk_test_..."
export ADMIN_PASSWORD="your-strong-password"
export SECRET_KEY="long-random-string"

python app.py
```

Then open:
- **Booking page**: http://localhost:5000/
- **Admin**: http://localhost:5000/admin

> Without Stripe keys the app still runs in **dev mode** (direct booking without payment) so you can test the UI.

## Getting Stripe Keys

1. Create a free account at [dashboard.stripe.com](https://dashboard.stripe.com)
2. Go to **Developers → API keys**
3. Copy the **Secret key** (`sk_test_...` or `sk_live_...`) and **Publishable key** (`pk_test_...` / `pk_live_...`)
4. Set them as environment variables (see above)

**Test card numbers** (Stripe test mode):
- Success: `4242 4242 4242 4242`
- Any future expiry, any CVC, any ZIP

## How the payment flow works

1. Guest selects date → available start times → duration (prices shown live)
2. Enters name, phone, email, party size
3. Clicks **Pay & Reserve**
4. Backend creates a Stripe Checkout Session with the exact amount + booking metadata
5. Guest is redirected to Stripe’s secure page and pays
6. Stripe redirects back to `/booking/success?session_id=...`
7. Backend verifies the payment was successful, writes the booking to the database, and shows a confirmation
8. That time range is now permanently blocked for everyone else

If someone abandons the Stripe page, the slot stays free (nothing is reserved until money actually clears).

## Production Deployment

### Recommended free/cheap hosts
- **Render.com** (Web Service)
- **Railway.app**
- **Fly.io**
- Any VPS + systemd / Docker

Set these environment variables on the host:

| Variable                  | Required | Notes |
|---------------------------|----------|-------|
| `STRIPE_SECRET_KEY`       | Yes      | `sk_live_...` for real money |
| `STRIPE_PUBLISHABLE_KEY`  | Yes      | `pk_live_...` |
| `ADMIN_PASSWORD`          | Yes      | Staff login |
| `SECRET_KEY`              | Yes      | Random long string for sessions |
| `PORT`                    | Usually  | Host will often set this automatically |

### Docker example
```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=5000
CMD ["python", "app.py"]
```

## Staff workflow

- Bookmark `/admin`
- See every upcoming reservation with **amount paid**, name, phone, notes
- Cancel with one click (the time instantly becomes available again)
- For refunds: go to the Stripe Dashboard → Payments → Refund (or we can add one-click refund later)

## File structure

```
karaoke-reservation/
├── app.py
├── requirements.txt          # flask + stripe
├── templates/
│   ├── index.html            # Public booking + pricing + Stripe redirect
│   ├── success.html          # Post-payment confirmation
│   ├── admin_login.html
│   └── admin.html
├── data/                     # SQLite DB (auto-created)
├── start.sh
├── Procfile
└── README.md
```

## Customization

- Change hourly rate: edit `PRICE_PER_HOUR_CENTS = 4000` in `app.py` (4000 = $40)
- Allowed durations / slot interval: top of `app.py`
- Branding / colors: Tailwind classes in the HTML templates
- Want automatic email receipts beyond Stripe’s? Easy to add later
- Want SMS confirmation or calendar invite? Just ask

## Security notes

- Card data never touches your server (Stripe Checkout)
- Booking is only written after Stripe confirms `payment_status === "paid"`
- Idempotent: refreshing the success page won’t create duplicate bookings
- Admin password is just a simple shared secret — change it and keep the URL private

---

Built for The Garden Lounge.  
Need refunds in the admin panel, multi-room support, deposits, or Google Calendar sync? Tell me and I’ll add it. 🎤🌿
