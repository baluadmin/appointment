import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from google import genai
from google.genai import types
import httpx

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("statai_bot")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "stataibooking2026")
OWNER_PHONE_NUMBER = os.getenv("OWNER_PHONE_NUMBER")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEET_WEBHOOK_URL = os.getenv("GOOGLE_SHEET_WEBHOOK_URL")

ai_client = genai.Client(api_key=GEMINI_API_KEY)
app = FastAPI(title="STATAI WhatsApp Booking Service")


# ---------------------------------------------------------
# Webhook Verification (Meta Handshake)
# ---------------------------------------------------------
@app.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge"),
):
    if mode == "subscribe" and token == VERIFY_TOKEN:
        logger.info("Webhook handshake verified successfully.")
        return challenge
    logger.warning("Webhook handshake failed. Token mismatch.")
    raise HTTPException(status_code=403, detail="Verification failed")


# ---------------------------------------------------------
# Webhook Receiver (Incoming WhatsApp Messages)
# ---------------------------------------------------------
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    try:
        entry = body.get("entry", [])[0]
        change = entry.get("changes", [])[0]["value"]
        if "messages" in change:
            message_obj = change["messages"][0]
            if message_obj.get("type") == "text":
                sender_id = message_obj["from"]
                user_text = message_obj["text"]["body"]
                background_tasks.add_task(handle_booking_pipeline, sender_id, user_text)
    except (IndexError, KeyError, TypeError) as err:
        logger.debug(f"Skipped non-message payload: {err}")

    return Response(status_code=200)


# ---------------------------------------------------------
# WhatsApp Cloud API Message Dispatcher
# ---------------------------------------------------------
async def send_whatsapp_message(to: str, message: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": message},
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.post(url, headers=headers, json=payload)
        if res.status_code >= 400:
            logger.error(f"Failed to send WhatsApp message to {to}: {res.text}")
        else:
            logger.info(f"Delivered WhatsApp message to {to}")


# ---------------------------------------------------------
# Google Sheets Logger
# ---------------------------------------------------------
async def log_to_google_sheet(name: str, phone: str, email: str, slot: str, notes: str) -> bool:
    if not GOOGLE_SHEET_WEBHOOK_URL:
        logger.error("GOOGLE_SHEET_WEBHOOK_URL is not set.")
        return False

    payload = {
        "name": name,
        "phone": phone,
        "email": email,
        "slot": slot,
        "notes": notes,
    }

    # follow_redirects=True is necessary for Google Apps Script redirects
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        res = await client.post(GOOGLE_SHEET_WEBHOOK_URL, json=payload)
        if res.status_code == 200:
            logger.info("Successfully recorded row in Google Sheet.")
            return True
        logger.error(f"Failed to log into Google Sheet: {res.status_code} - {res.text}")
        return False


# ---------------------------------------------------------
# Gemini Function Calling Definition
# ---------------------------------------------------------
booking_tool = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="record_appointment",
            description="Call when customer provides Name, Email, and Preferred Date/Time for appointment.",
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "name": types.Schema(type=types.Type.STRING, description="Customer full name"),
                    "email": types.Schema(type=types.Type.STRING, description="Customer email address"),
                    "slot": types.Schema(
                        type=types.Type.STRING,
                        description="Requested date and time in readable format (e.g., 2026-09-15 04:00 PM)",
                    ),
                    "notes": types.Schema(type=types.Type.STRING, description="Service needed or extra notes"),
                },
                required=["name", "email", "slot"],
            ),
        )
    ]
)


async def handle_booking_pipeline(sender_id: str, incoming_text: str):
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %Y-%m-%d %I:%M %p")

    system_instruction = f"""You are the automated booking assistant for STAT AI.
Current Date & Time: {now_ist} (Timezone: Asia/Kolkata).

Instructions:
1. Greet politely and help customers book their appointment.
2. Require three items: Customer Name, Email, and Preferred Date/Time.
3. If details are missing, ask specifically for the missing item in one friendly sentence.
4. When all three details are provided, invoke `record_appointment`.
5. Keep general conversational replies brief and concise for WhatsApp.
"""

    response = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=incoming_text,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[booking_tool],
            temperature=0.3,
        ),
    )

    if response.function_calls:
        call = response.function_calls[0]
        args = call.args

        name = args.get("name")
        email = args.get("email")
        slot = args.get("slot")
        notes = args.get("notes", "WhatsApp Appointment")

        success = await log_to_google_sheet(name, sender_id, email, slot, notes)

        if success:
            customer_reply = (
                f"Booking confirmed!\n\n"
                f"Name: {name}\n"
                f"Slot: {slot}\n\n"
                f"Our team has logged your appointment and will reach out to {email} shortly."
            )
            owner_alert = (
                f"*New Booking Added to Google Sheet*\n"
                f"Customer: {name}\n"
                f"Phone: +{sender_id}\n"
                f"Email: {email}\n"
                f"Requested Slot: {slot}\n"
                f"Notes: {notes}"
            )
            await send_whatsapp_message(sender_id, customer_reply)
            if OWNER_PHONE_NUMBER:
                await send_whatsapp_message(OWNER_PHONE_NUMBER, owner_alert)
        else:
            await send_whatsapp_message(
                sender_id,
                "We encountered a temporary technical glitch while logging your appointment. Please try again shortly.",
            )
    else:
        reply = response.text
        if reply:
            await send_whatsapp_message(sender_id, reply)
