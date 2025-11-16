# support_bot_api.py

import os
from typing import List, Optional, Literal

import httpx
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# ---------- Types / Models ----------

SupportContext = Literal["jobs", "classifieds"]


class SupportBotRequest(BaseModel):
  userId: Optional[str] = None
  message: str
  context: SupportContext


class SupportBotReply(BaseModel):
  role: str = "bot"
  name: str = "Road Workers Connect Support Bot"
  reply: str
  followUpQuestions: Optional[List[str]] = None
  issueReported: bool = False


class WelcomeRequest(BaseModel):
  userId: str  # new human user's Supabase user id


# ---------- App Setup ----------

app = FastAPI(title="Road Workers Connect - Support & Welcome Bot")

origins = [
  "https://app.superiorllc.org",
  "https://superiorllc.org",
  # add Famous.AI / Netlify preview URLs here if needed
]

app.add_middleware(
  CORSMiddleware,
  allow_origins=origins,
  allow_credentials=True,
  allow_methods=["POST", "OPTIONS"],
  allow_headers=["*"],
)

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
SUPPORT_FROM_EMAIL = os.getenv("SUPPORT_FROM_EMAIL", "no-reply@superiorllc.org")
SUPPORT_TO_EMAIL = os.getenv("SUPPORT_TO_EMAIL", "info@superiorllc.org")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SENDGRID_API_KEY:
  raise RuntimeError("SENDGRID_API_KEY not set")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
  raise RuntimeError("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set")

SUPABASE_REST_URL = SUPABASE_URL.rstrip("/") + "/rest/v1"


# ---------- Support Bot Logic ----------

def looks_like_environment_issue(message: str) -> bool:
  lower = message.lower()
  triggers = [
    "not loading",
    "spinner",
    "crash",
    "error",
    "404",
    "500",
    "502",
    "cannot connect",
    "connection problem",
    "network issue",
    "link not working",
    "broken link",
    "app froze",
    "white screen",
  ]
  return any(t in lower for t in triggers)


def build_jobs_help(message: str) -> SupportBotReply:
  lower = message.lower()

  if "post" in lower and "job" in lower:
    return SupportBotReply(
      reply=(
        "To post a job, tap “Post a Job”, fill in job title, location, pay range, any "
        "required certifications, and shift details. When you save, your listing appears "
        "on the Job Board."
      ),
      followUpQuestions=[
        "Do you want help with the job title or description?",
        "Is this a short call-out or a longer-term position?",
      ],
    )

  if "edit" in lower or "update" in lower:
    return SupportBotReply(
      reply=(
        "To edit a job, open your job post, choose “Edit”, adjust the details, and save. "
        "The Job Board updates automatically."
      )
    )

  if "delete" in lower or "remove" in lower:
    return SupportBotReply(
      reply=(
        "To remove a job, open the post and choose “Delete”. Once removed, it will no "
        "longer appear on the Job Board."
      )
    )

  return SupportBotReply(
    reply=(
      "I can help you create, edit, or remove job posts. Tell me if you’re posting a new "
      "job, updating one, or taking one down."
    )
  )


def build_classifieds_help(message: str) -> SupportBotReply:
  lower = message.lower()

  if "post" in lower and any(w in lower for w in ["item", "equipment", "tool", "tools"]):
    return SupportBotReply(
      reply=(
        "To post an item for sale, tap “New Classified”, add photos, a clear title, "
        "condition, price, and pickup or delivery details. When you publish, it appears "
        "in the Classifieds feed."
      ),
      followUpQuestions=[
        "Are you listing tools, PPE, or heavy equipment?",
        "Do you want advice on pricing or description?",
      ],
    )

  if "mark" in lower and "sold" in lower:
    return SupportBotReply(
      reply=(
        "To mark an item as sold, open your classified post and tap “Mark as Sold”. "
        "Other users will see that it is no longer available."
      )
    )

  return SupportBotReply(
    reply=(
      "I can walk you through posting items for sale, editing listings, or marking them "
      "as sold. What are you trying to do?"
    )
  )


def send_issue_email_sync(req: SupportBotRequest) -> None:
  text_lines = [
    "Support issue reported from Road Workers Connect:",
    "",
    f"Context: {req.context}",
    f"User ID: {req.userId or 'unknown / guest'}",
    "",
    "User message:",
    req.message,
  ]
  message = Mail(
    from_email=SUPPORT_FROM_EMAIL,
    to_emails=SUPPORT_TO_EMAIL,
    subject="[Road Workers Connect] User-reported issue",
    plain_text_content="\n".join(text_lines),
  )
  sg = SendGridAPIClient(SENDGRID_API_KEY)
  sg.send(message)


async def handle_support_message(req: SupportBotRequest, background_tasks: BackgroundTasks) -> SupportBotReply:
  if looks_like_environment_issue(req.message):
    background_tasks.add_task(send_issue_email_sync, req)
    return SupportBotReply(
      reply=(
        "I’m Road Workers Connect Support Bot. It looks like you’re running into a "
        "technical problem. We’re aware of the problem you’re having and we will address "
        "it immediately. If it continues, you can also email info@superiorllc.org."
      ),
      issueReported=True,
    )

  if req.context == "jobs":
    return build_jobs_help(req.message)

  return build_classifieds_help(req.message)


@app.post("/support-bot", response_model=SupportBotReply)
async def support_bot_endpoint(req: SupportBotRequest, background_tasks: BackgroundTasks):
  try:
    return await handle_support_message(req, background_tasks)
  except Exception as e:
    print("[SupportBot] error:", repr(e))
    return SupportBotReply(
      reply=(
        "I’m Road Workers Connect Support Bot. Something went wrong on our side. We’re "
        "aware of the problem you’re having and we will address it immediately."
      ),
      issueReported=True,
    )


# ---------- Moderator Welcome DM Logic ----------

async def get_moderator_bot_id(client: httpx.AsyncClient) -> Optional[str]:
  """
  Look up the moderator bot from public.users via Supabase REST.
  """
  url = f"{SUPABASE_REST_URL}/users"
  params = {
    "is_bot": "eq.true",
    "bot_role": "eq.moderator",
    "select": "id",
    "limit": 1,
  }
  headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
  }

  resp = await client.get(url, params=params, headers=headers, timeout=10)
  resp.raise_for_status()
  data = resp.json()
  if not data:
    return None
  return data[0]["id"]


def build_welcome_message_text() -> str:
  return (
    "Welcome to Road Workers Connect. This space is for road crews, contractors, and "
    "industry pros to trade jobs, gear, and good information.\n\n"
    "Keep it professional, avoid spam, and stay focused on real work. If you need help "
    "posting jobs or items for sale, tap the Support button and the Support Bot will walk "
    "you through it.\n\n"
    "Stay safe out there."
  )


async def send_welcome_dm(new_user_id: str, client: httpx.AsyncClient) -> None:
  moderator_id = await get_moderator_bot_id(client)
  if not moderator_id:
    print("[WelcomeBot] No moderator bot found (is_bot = true, bot_role = 'moderator')")
    return

  url = f"{SUPABASE_REST_URL}/messages"
  headers = {
    "apikey": SUPABASE_SERVICE_ROLE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
  }
  payload = {
    "sender_id": moderator_id,
    "recipient_id": new_user_id,
    "content": build_welcome_message_text(),
    "is_bot_generated": True,
  }

  resp = await client.post(url, headers=headers, json=payload, timeout=10)
  resp.raise_for_status()
  print(f"[WelcomeBot] Welcome DM inserted for user {new_user_id}")


@app.post("/welcome-message")
async def welcome_message_endpoint(req: WelcomeRequest):
  """
  Call this after a new Supabase user is created.
  Sends a DM from the moderator bot to the new user.
  """
  if not req.userId:
    return {"status": "error", "message": "userId is required"}

  try:
    async with httpx.AsyncClient() as client:
      await send_welcome_dm(req.userId, client)
    return {"status": "ok"}
  except Exception as e:
    print("[WelcomeBot] error:", repr(e))
    # Do not block signup flow; just log
    return {"status": "error", "message": "failed to send welcome message"}
