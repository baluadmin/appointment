import os
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI(title="STAT AI WhatsApp Booking Bot")

# Environment Variables
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1196410380229543")
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "stataibooking2026")

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

# User state tracker: { phone_number: {"step": int, "data": dict} }
user_sessions = {}


@app.get("/")
@app.head("/")
async def health_check():
    """Health check route so Render does not shut down the web service."""
    return {"status": "ok", "service": "whatsapp-booking-bot"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    """Meta webhook verification challenge."""
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("INFO: statai_bot.Webhook handshake verified successfully.")
        return Response(content=challenge, media_type="text/plain")

    return Response(content="Verification token mismatch", status_code=403)


async def send_text(to: str, text: str):
    """Sends a standard text message back to WhatsApp."""
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(GRAPH_URL, json=payload, headers=HEADERS)
        if res.status_code >= 400:
            print(f"Meta Send Text Error [{res.status_code}]: {res.text}")


async def send_buttons(to: str, body_text: str, buttons: list):
    """Sends up to 3 interactive reply buttons."""
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
            "body": {"text": body_text},
            "action": {"buttons": button_elements}
        }
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(GRAPH_URL, json=payload, headers=HEADERS)
        if res.status_code >= 400:
            print(f"Meta Send Buttons Error [{res.status_code}]: {res.text}")


async def log_to_google_sheet(payload: dict):
    """Posts customer booking details to Google Apps Script webhook."""
    if not GOOGLE_SHEET_WEBHOOK_URL:
        print("WARNING: GOOGLE_SHEET_WEBHOOK_URL is not set. Skipping sheet log.")
        return
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            res = await client.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=10.0)
            print(f"Sheet Response [{res.status_code}]: {res.text}")
        except Exception as e:
            print(f"Google Sheet logging failed: {e}")


@app.post("/webhook")
async def handle_whatsapp(request: Request):
    """Handles incoming WhatsApp events and manages the booking state machine."""
    body = await request.json()

    try:
        entry = body.get("entry", [])[0]
        change = entry.get("changes", [])[0]["value"]

        # Ignore delivery status receipts (sent, delivered, read)
        if "messages" not in change:
            return {"status": "ignored"}

        msg = change["messages"][0]
        sender = msg["from"]

        # Parse text or button clicks
        user_input = ""
        button_id = ""
        if msg.get("type") == "text":
            user_input = msg["text"]["body"].strip()
        elif msg.get("type") == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_id = interactive["button_reply"]["id"]
                user_input = interactive["button_reply"]["title"]

        # Get or initialize user state
        session = user_sessions.setdefault(sender, {"step": 0, "data": {}})
        current_step = session["step"]

        # Reset command or brand-new conversation
        if user_input.lower() in ["hi", "hello", "restart", "start", "menu"] or current_step == 0:
            session["step"] = 1
            session["data"] = {}
            await send_buttons(
                sender,
                "Welcome to STAT AI! Would you like to schedule an appointment?",
                [
                    {"id": "btn_book", "title": "Book Appointment"},
                    {"id": "btn_info", "title": "About Us"}
                ]
            )
            return {"status": "ok"}

        # Step 1: Handle Book vs About
        if current_step == 1:
            if button_id == "btn_book" or "book" in user_input.lower():
                session["step"] = 2
                await send_buttons(
                    sender,
                    "Select a date for your appointment:",
                    [
                        {"id": "slot_today", "title": "Today"},
                        {"id": "slot_tomorrow", "title": "Tomorrow"},
                        {"id": "slot_upcoming", "title": "Upcoming Monday"}
                    ]
                )
            else:
                await send_text(
                    sender,
                    "STAT AI builds automated customer systems and integrations. Send 'Hi' anytime to book a slot!"
                )
                session["step"] = 0
            return {"status": "ok"}

        # Step 2: Handle Date selection -> Show Times
        if current_step == 2:
            session["data"]["date"] = user_input
            session["step"] = 3
            await send_buttons(
                sender,
                f"Date: {user_input}\nChoose a time slot:",
                [
                    {"id": "time_11am", "title": "11:00 AM"},
                    {"id": "time_03pm", "title": "03:00 PM"},
                    {"id": "time_06pm", "title": "06:00 PM"}
                ]
            )
            return {"status": "ok"}

        # Step 3: Handle Time selection -> Prompt contact info
        if current_step == 3:
            session["data"]["time"] = user_input
            session["step"] = 4
            await send_text(
                sender,
                f"Slot selected: {session['data']['date']} at {user_input}.\n\n"
                "Please reply with your Name and Email separated by a comma.\n"
                "Example: *Balu, balu@example.com*"
            )
            return {"status": "ok"}

        # Step 4: Parse Name/Email, write to Google Sheet, and finish
        if current_step == 4:
            parts = [p.strip() for p in user_input.split(",")]
            name = parts[0] if len(parts) > 0 and parts[0] else "Customer"
            email = parts[1] if len(parts) > 1 and parts[1] else "Not provided"

            booking_payload = {
                "name": name,
                "phone": sender,
                "email": email,
                "dateTime": f"{session['data'].get('date')} at {session['data'].get('time')}",
                "status": "Confirmed"
            }

            # Submit to Apps Script Webhook
            await log_to_google_sheet(booking_payload)

            # Send final confirmation message to the user
            await send_text(
                sender,
                f"🎉 *Booking Confirmed!*\n\n"
                f"• *Name:* {name}\n"
                f"• *Phone:* +{sender}\n"
                f"• *Email:* {email}\n"
                f"• *Appointment:* {booking_payload['dateTime']}\n\n"
                f"Thank you for choosing STAT AI! Reply 'Hi' anytime to start a new booking."
            )

            # Reset session state
            session["step"] = 0
            session["data"] = {}
            return {"status": "ok"}

    except Exception as e:
        print(f"Error executing webhook: {e}")

    return {"status": "ok"}
