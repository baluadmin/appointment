import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
import httpx
from openai import OpenAI

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("statai_bot")

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "stataibooking2026")
OWNER_PHONE_NUMBER = os.getenv("OWNER_PHONE_NUMBER")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CAL_API_KEY = os.getenv("CAL_API_KEY")
CAL_EVENT_TYPE_ID = int(os.getenv("CAL_EVENT_TYPE_ID", "0"))

openai_client = OpenAI(api_key=OPENAI_API_KEY)
app = FastAPI(title="STATAI WhatsApp Booking Service")


# ---------------------------------------------------------
# Webhook Verification (Handshake with Meta)
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
        logger.debug(f"Non-message event skipped: {err}")

    return Response(status_code=200)


# ---------------------------------------------------------
# WhatsApp Cloud API Message Sender
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
            logger.error(f"Failed to send message to {to}: {res.text}")
        else:
            logger.info(f"Message delivered to {to}")


# ---------------------------------------------------------
# Cal.com Booking Creation
# ---------------------------------------------------------
async def create_cal_booking(name: str, email: str, start_iso: str, notes: str) -> bool:
    url = "https://api.cal.com/v2/bookings"
    headers = {
        "Authorization": f"Bearer {CAL_API_KEY}",
        "Content-Type": "application/json",
        "cal-api-version": "2026-02-25",
    }
    payload = {
        "eventTypeId": CAL_EVENT_TYPE_ID,
        "start": start_iso,
        "attendee": {
            "name": name,
            "email": email,
            "timeZone": "Asia/Kolkata",
            "language": "en",
        },
        "metadata": {"notes": notes},
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        res = await client.post(url, headers=headers, json=payload)
        if res.status_code in [200, 201]:
            return True
        logger.error(f"Cal.com booking failed: {res.status_code} - {res.text}")
        return False


# ---------------------------------------------------------
# OpenAI Function Calling & Dialog Engine
# ---------------------------------------------------------
booking_tool = {
    "type": "function",
    "function": {
        "name": "book_appointment",
        "description": "Call this when the user has supplied their name, email, and preferred appointment start time.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Customer full name"},
                "email": {"type": "string", "description": "Customer email address"},
                "start_time_iso": {
                    "type": "string",
                    "description": "Start date-time in ISO 8601 format (e.g., 2026-09-14T15:00:00+05:30)",
                },
                "notes": {"type": "string", "description": "Service needed or discussion notes"},
            },
            "required": ["name", "email", "start_time_iso"],
        },
    },
}


async def handle_booking_pipeline(sender_id: str, incoming_text: str):
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A, %Y-%m-%d %I:%M %p")

    system_prompt = f"""You are the automated booking assistant for STAT AI.
Current Date & Time: {now_ist} (Timezone: Asia/Kolkata).

Rules:
1. Greet politely and help customers book appointments.
2. To book, you need: Customer Name, Email, and Preferred Date/Time.
3. If information is missing, ask for only what is missing in a single short sentence.
4. When all three details are provided, invoke the `book_appointment` tool. Calculate the start_time_iso accurately according to the current date and time provided.
5. Keep conversational replies brief and friendly for WhatsApp.
"""

    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": incoming_text},
        ],
        tools=[booking_tool],
        tool_choice="auto",
    )

    choice = response.choices[0].message

    if choice.tool_calls:
        tool_call = choice.tool_calls[0]
        args = json.loads(tool_call.function.arguments)

        name = args.get("name")
        email = args.get("email")
        start_iso = args.get("start_time_iso")
        notes = args.get("notes", "STAT AI WhatsApp Appointment")

        success = await create_cal_booking(name, email, start_iso, notes)

        if success:
            customer_msg = (
                f"Booking confirmed!\n\n"
                f"Name: {name}\n"
                f"Slot: {start_iso}\n"
                f"A calendar invitation has been sent to {email}."
            )
            owner_msg = (
                f"*New Appointment Confirmed*\n"
                f"Customer: {name}\n"
                f"Phone: +{sender_id}\n"
                f"Email: {email}\n"
                f"Scheduled for: {start_iso}"
            )
            await send_whatsapp_message(sender_id, customer_msg)
            if OWNER_PHONE_NUMBER:
                await send_whatsapp_message(OWNER_PHONE_NUMBER, owner_msg)
        else:
            await send_whatsapp_message(
                sender_id,
                "We could not confirm that specific time slot. Please choose another date or time.",
            )
    else:
        reply = choice.content
        await send_whatsapp_message(sender_id, reply)
