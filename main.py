import os
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI(title="WhatsApp Booking Backend")

# Environment configurations
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1196410380229543")
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "stataibooking2026")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# In-memory session store: { sender_id: {"step": int, "data": dict} }
user_sessions = {}


@app.get("/")
@app.head("/")
async def root_health_check():
    """Health check route to keep the hosting service active."""
    return {"status": "ok", "service": "whatsapp-booking-bot"}


@app.get("/webhook")
async def verify_meta_webhook(request: Request):
    """Meta webhook challenge verification handshake."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Forbidden", status_code=403)


async def send_to_meta(payload: dict):
    """Dispatches outgoing requests to Meta Graph API."""
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(GRAPH_URL, json=payload, headers=headers)
        if response.status_code >= 400:
            print(f"Meta API Error [{response.status_code}]: {response.text}")
        return response


async def send_text(to: str, message: str):
    """Sends a plain text message to WhatsApp user."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }
    await send_to_meta(payload)


async def send_buttons(to: str, text: str, buttons: list):
    """Sends interactive reply buttons (max 3)."""
    button_elements = [
        {"type": "reply", "reply": {"id": btn["id"], "title": btn["title"]}}
        for btn in buttons[:3]
    ]
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {"buttons": button_elements},
        },
    }
    await send_to_meta(payload)


async def forward_to_google_sheet(payload: dict):
    """Submits booked customer record to Google Apps Script."""
    if not GOOGLE_SHEET_WEBHOOK_URL:
        print("Warning: GOOGLE_SHEET_WEBHOOK_URL is not configured.")
        return

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=10.0)
            print(f"Sheet Sync [{res.status_code}]: {res.text}")
        except Exception as e:
            print(f"Failed to post to Google Sheets: {e}")


@app.post("/webhook")
async def receive_webhook(request: Request):
    """Processes incoming WhatsApp messages and runs deterministic booking steps."""
    body = await request.json()

    try:
        entry = body.get("entry", [])[0]
        changes = entry.get("changes", [])[0]["value"]

        # Ignore non-message updates (sent/read/delivered receipts)
        if "messages" not in changes:
            return {"status": "ignored"}

        msg = changes["messages"][0]
        sender = msg["from"]

        # Parse message content
        user_input = ""
        button_id = ""

        if msg.get("type") == "text":
            user_input = msg["text"]["body"].strip()
        elif msg.get("type") == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_id = interactive["button_reply"]["id"]
                user_input = interactive["button_reply"]["title"]

        session = user_sessions.setdefault(sender, {"step": 0, "data": {}})
        step = session["step"]
        normalized_input = user_input.lower()

        # Step 0: Initial contact or reset
        if normalized_input in ["hi", "hello", "restart", "start", "menu"] or step == 0:
            session["step"] = 1
            session["data"] = {}
            await send_buttons(
                sender,
                "Welcome to STAT AI! Would you like to schedule an appointment?",
                [
                    {"id": "btn_book", "title": "Book Appointment"},
                    {"id": "btn_info", "title": "About Us"},
                ],
            )
            return {"status": "ok"}

        # Step 1: Menu selection
        if step == 1:
            if button_id == "btn_book" or "book" in normalized_input:
                session["step"] = 2
                await send_buttons(
                    sender,
                    "Select an appointment day:",
                    [
                        {"id": "slot_today", "title": "Today"},
                        {"id": "slot_tomorrow", "title": "Tomorrow"},
                        {"id": "slot_upcoming", "title": "Upcoming Monday"},
                    ],
                )
            else:
                await send_text(
                    sender,
                    "STAT AI automates business operations and messaging workflows. Reply 'Hi' anytime to book an appointment.",
                )
                session["step"] = 0
            return {"status": "ok"}

        # Step 2: Date selection
        if step == 2:
            session["data"]["date"] = user_input
            session["step"] = 3
            await send_buttons(
                sender,
                f"Selected Date: {user_input}\nChoose an available time slot:",
                [
                    {"id": "time_11am", "title": "11:00 AM"},
                    {"id": "time_03pm", "title": "03:00 PM"},
                    {"id": "time_06pm", "title": "06:00 PM"},
                ],
            )
            return {"status": "ok"}

        # Step 3: Time selection
        if step == 3:
            session["data"]["time"] = user_input
            session["step"] = 4
            await send_text(
                sender,
                f"Slot reserved: {session['data']['date']} at {user_input}.\n\n"
                "Please reply with your Name and Email separated by a comma.\n"
                "Example: *Balu, balu@example.com*",
            )
            return {"status": "ok"}

        # Step 4: Contact details capture & spreadsheet export
        if step == 4:
            parts = [part.strip() for part in user_input.split(",")]
            name = parts[0] if len(parts) > 0 and parts[0] else "Customer"
            email = parts[1] if len(parts) > 1 and parts[1] else "Not provided"
            date_time = f"{session['data'].get('date')} at {session['data'].get('time')}"

            sheet_payload = {
                "name": name,
                "phone": sender,
                "email": email,
                "dateTime": date_time,
                "status": "Confirmed",
            }

            # Forward to Apps Script
            await forward_to_google_sheet(sheet_payload)

            # Send WhatsApp confirmation receipt
            await send_text(
                sender,
                f"🎉 *Booking Confirmed!*\n\n"
                f"• *Name:* {name}\n"
                f"• *Phone:* +{sender}\n"
                f"• *Email:* {email}\n"
                f"• *Schedule:* {date_time}\n\n"
                f"Thank you for choosing STAT AI! Reply 'Hi' anytime to start a new booking.",
            )

            # Reset session
            session["step"] = 0
            session["data"] = {}
            return {"status": "ok"}

    except Exception as exc:
        print(f"Webhook processing error: {exc}")

    return {"status": "ok"}
