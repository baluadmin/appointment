import os
import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1196410380229543")
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "stataibooking2026")

# In-memory user session tracker { "phone_number": {"step": ..., "data": {...}} }
user_sessions = {}

GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
HEADERS = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=params.get("hub.challenge"), media_type="text/plain")
    return Response(content="Verification failed", status_code=403)

async def send_text(to: str, text: str):
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    async with httpx.AsyncClient() as client:
        await client.post(GRAPH_URL, json=payload, headers=HEADERS)

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
        await client.post(GRAPH_URL, json=payload, headers=HEADERS)

async def log_to_google_sheet(payload: dict):
    if not GOOGLE_SHEET_WEBHOOK_URL:
        return
    async with httpx.AsyncClient() as client:
        try:
            await client.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload, timeout=10.0)
        except Exception as e:
            print(f"Sheet error: {e}")

@app.post("/webhook")
async def handle_whatsapp(request: Request):
    body = await request.json()
    
    try:
        entry = body.get("entry", [])[0]
        change = entry.get("changes", [])[0]["value"]
        if "messages" not in change:
            return {"status": "ignored"}
        
        msg = change["messages"][0]
        sender = msg["from"]
        
        # Extract message content (text or interactive button tap)
        user_input = ""
        button_id = ""
        if msg.get("type") == "text":
            user_input = msg["text"]["body"].strip()
        elif msg.get("type") == "interactive":
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                button_id = interactive["button_reply"]["id"]
                user_input = interactive["button_reply"]["title"]

        # Retrieve current user state
        session = user_sessions.setdefault(sender, {"step": 0, "data": {}})
        current_step = session["step"]

        # State 0: Welcome / Reset
        if user_input.lower() in ["hi", "hello", "restart", "cancel"] or current_step == 0:
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

        # State 1: Action Menu response
        if current_step == 1:
            if button_id == "btn_book" or "book" in user_input.lower():
                session["step"] = 2
                await send_buttons(
                    sender,
                    "Please choose a day for your appointment:",
                    [
                        {"id": "slot_today", "title": "Today"},
                        {"id": "slot_tomorrow", "title": "Tomorrow"},
                        {"id": "slot_monday", "title": "Upcoming Monday"}
                    ]
                )
            else:
                await send_text(sender, "STAT AI provides automated AI and messaging integrations. Send 'Hi' anytime to book a consultation!")
                session["step"] = 0
            return {"status": "ok"}

        # State 2: Day chosen -> choose time slot
        if current_step == 2:
            session["data"]["date"] = user_input
            session["step"] = 3
            await send_buttons(
                sender,
                f"Selected: {user_input}.\nNow select a time slot:",
                [
                    {"id": "time_11am", "title": "11:00 AM"},
                    {"id": "time_03pm", "title": "03:00 PM"},
                    {"id": "time_06pm", "title": "06:00 PM"}
                ]
            )
            return {"status": "ok"}

        # State 3: Time chosen -> collect contact info
        if current_step == 3:
            session["data"]["time"] = user_input
            session["step"] = 4
            await send_text(
                sender,
                f"Slot reserved for {session['data']['date']} at {user_input}.\n\n"
                "Please reply with your Name and Email address separated by a comma.\n"
                "Example: *Balu, balu@example.com*"
            )
            return {"status": "ok"}

        # State 4: Record details and post to Google Sheets
        if current_step == 4:
            parts = [p.strip() for p in user_input.split(",")]
            name = parts[0] if len(parts) > 0 else "Customer"
            email = parts[1] if len(parts) > 1 else "Not provided"

            booking_payload = {
                "name": name,
                "phone": sender,
                "email": email,
                "dateTime": f"{session['data'].get('date')} at {session['data'].get('time')}",
                "status": "Confirmed"
            }

            # Post directly to Google Sheet
            await log_to_google_sheet(booking_payload)

            # Confirm to the user
            await send_text(
                sender,
                f"🎉 Booking Confirmed!\n\n"
                f"• Name: {name}\n"
                f"• Phone: +{sender}\n"
                f"• Email: {email}\n"
                f"• Scheduled: {booking_payload['dateTime']}\n\n"
                f"Thank you for choosing STAT AI. Send 'Hi' anytime to start a new booking."
            )
            # Reset session
            session["step"] = 0
            session["data"] = {}
            return {"status": "ok"}

    except Exception as e:
        print(f"Error handling message: {e}")

    return {"status": "ok"}
