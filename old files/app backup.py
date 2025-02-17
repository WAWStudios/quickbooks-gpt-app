import eventlet
eventlet.monkey_patch()

import os
import logging
import requests
import secrets
import random
import re, sys, threading
import jwt
from jwt import ExpiredSignatureError, InvalidTokenError
import string
import bcrypt
import time
import atexit
import eventlet
from flask import render_template, redirect, request, Response, stream_with_context, make_response, url_for, jsonify, Flask, session, send_from_directory, abort
from flask_mail import Mail, Message
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from gotrue.errors import AuthApiError  # Correct import for error handling
from bcrypt import checkpw
from openai import OpenAI, AssistantEventHandler
import stripe
from functools import wraps
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_ERROR
from urllib.parse import quote
import uuid  # Added for chat_session_id generation

#------------------- Open AI Assistant ID -------------------#
ASSISTANT_ID = "asst_0w2HuDpG8cgKC3liBnHtUWSO"

# ✅ Store user threads in memory (for now)
user_threads = {}
#------------------------------------------------------------#

# ------------------------------------------------------------------------------
# Environment variables
# ------------------------------------------------------------------------------

# Load environment variables from .env file in development mode
if os.getenv("FLASK_ENV") == "development":
    load_dotenv()

DEV_MODE = os.getenv("FLASK_ENV", "production") == "development"

# Validate JWT_SECRET_KEY
SECRET_KEY = os.getenv("JWT_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY environment variable is missing.")

# Define required environment variables
required_env_vars = ['SUPABASE_URL', 'SUPABASE_KEY', 'FLASK_SECRET_KEY']
if DEV_MODE:
    required_env_vars.extend(['QB_SANDBOX_CLIENT_ID', 'QB_SANDBOX_CLIENT_SECRET', 'SANDBOX_REDIRECT_URI'])
else:
    required_env_vars.extend(['QB_PROD_CLIENT_ID', 'QB_PROD_CLIENT_SECRET', 'PROD_REDIRECT_URI'])

# Check for missing environment variables
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise RuntimeError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Print loaded environment variables (masking sensitive ones)
print("Loaded Environment Variables:")
for key in required_env_vars:
    value = os.getenv(key)
    # Mask out secrets
    print(f"{key}: {'*****' if 'KEY' in key or 'SECRET' in key else value}")

# ------------------------------------------------------------------------------
# Logging Configuration
# ------------------------------------------------------------------------------

log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logging.info(f"Running in {os.getenv('FLASK_ENV', 'unknown')} mode.")

# ------------------------------------------------------------------------------
# Supabase Initialization
# ------------------------------------------------------------------------------

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')

try:
    client_options = ClientOptions(postgrest_client_timeout=30)
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY, options=client_options)
    logging.info("Supabase client initialized successfully.")
except Exception as e:
    logging.error(f"Error initializing Supabase client: {e}")
    supabase = None
    raise e  # It's critical to have Supabase initialized

# ------------------------------------------------------------------------------
# Scheduler initialization
# ------------------------------------------------------------------------------

scheduler = BackgroundScheduler()

def cleanup_expired_states():
    """
    Deletes expired entries in 'chatgpt_oauth_states'.
    """
    try:
        now = datetime.utcnow().isoformat()
        supabase.table("chatgpt_oauth_states").delete().lt("expiry", now).execute()
        logging.info("Expired state tokens cleaned up.")
    except Exception as e:
        logging.error(f"Error cleaning up expired states: {e}")

def cleanup_expired_verifications():
    """
    Deletes expired email verifications from 'email_verifications'.
    """
    try:
        now = datetime.utcnow().isoformat()
        supabase.table("email_verifications").delete().lt("expires_at", now).execute()
        logging.info("Expired email verifications cleaned up.")
    except Exception as e:
        logging.error(f"Error cleaning up expired email verifications: {e}")

def cleanup_expired_verifications_and_pending_users():
    """
    Deletes expired email verifications and pending users.
    """
    try:
        now = datetime.utcnow().isoformat()

        # Delete expired email verifications
        supabase.table("email_verifications").delete().lt("expires_at", now).execute()
        logging.info("Expired email verifications cleaned up.")

        # Delete users with 'pending' subscription status and expired verifications
        expired_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        expired_users = supabase.table("user_profiles").select("id").eq("subscription_status", "pending").lt("created_at", expired_cutoff).execute()

        for user in expired_users.data:
            user_id = user['id']
            supabase.auth.api.delete_user(user_id)
            supabase.table("user_profiles").delete().eq("id", user_id).execute()
            logging.info(f"Deleted pending user {user_id} due to expired verification.")
    except Exception as e:
        logging.error(f"Error during cleanup: {e}")


def cleanup_inactive_users():
    """
    Deletes inactive user accounts created over 24 hours ago.
    """
    try:
        cutoff_time = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        supabase.table("user_profiles").delete().lt("created_at", cutoff_time).eq("subscription_status", "inactive").execute()
        logging.info("Inactive user accounts cleaned up.")
    except Exception as e:
        logging.error(f"Error cleaning up inactive user accounts: {e}")

def log_scheduler_error(event):
    """
    Logs errors occurring in scheduled jobs.
    """
    if event.exception:
        logging.error(f"Scheduler job failed: {event.job_id}, Exception: {event.exception}")

# Schedule cleanup jobs
scheduler.add_job(cleanup_expired_states, 'interval', hours=1, id='cleanup_expired_states')
scheduler.add_job(cleanup_expired_verifications, 'cron', hour=0, id='cleanup_expired_verifications')
scheduler.add_job(cleanup_expired_verifications_and_pending_users, 'interval', hours=1, id='cleanup_expired_verifications_and_pending_users')
scheduler.add_job(cleanup_inactive_users, 'cron', hour=1, id='cleanup_inactive_users')

# Register error listener
scheduler.add_listener(log_scheduler_error, EVENT_JOB_ERROR)

# Start the scheduler
scheduler.start()
logging.info("Scheduler started successfully.")

# Register scheduler shutdown
atexit.register(lambda: scheduler.shutdown())

# ------------------------------------------------------------------------------
# Flask app initialization
# ------------------------------------------------------------------------------
# ✅ Initialize Flask app
app = Flask(
    __name__,
    static_folder="../frontend/.svelte-kit/output/client",  # ✅ Svelte static files
    static_url_path="/",  
    template_folder="templates"
)

# ✅ Serve Flask's static files separately
app.static_folder = "static"  # ✅ Ensures Flask still serves /backend/static


# ✅ Set secret key for security (session management, CSRF protection, etc.)
app.secret_key = os.getenv('FLASK_SECRET_KEY')
if not app.secret_key:
    raise RuntimeError("Missing FLASK_SECRET_KEY environment variable.")

# ✅ Add cookie configuration here
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='None',
    SESSION_COOKIE_DOMAIN='.linkbooksai.com',
    SESSION_COOKIE_HTTPONLY=True
)


#------------Websocket Initialization-------------------#

# Enable CORS

# Get the correct CORS origin based on environment
CORS_ORIGIN = os.getenv("CORS_ORIGIN", "https://linkbooksai.com")  # Production URL
CORS_ORIGIN_LOCAL = os.getenv("CORS_ORIGIN_LOCAL", "http://localhost:5173")  # Local Dev URL

# Determine active CORS origin
ACTIVE_CORS_ORIGIN = CORS_ORIGIN_LOCAL if os.getenv("FLASK_ENV") == "development" else CORS_ORIGIN

# ✅ CORS Configurations
ALLOWED_CORS_ORIGINS = [
    "https://linkbooksai.com",
    "https://app.linkbooksai.com"
]

# ✅ Include localhost only in development mode
if os.getenv("FLASK_ENV") == "development":
    ALLOWED_CORS_ORIGINS.append("http://localhost:5173")

print(f"✅ Using CORS Origin: {ACTIVE_CORS_ORIGIN}")  # Debugging log

# ✅ Enable CORS for HTTP Requests  
CORS(
    app,  
    supports_credentials=True,  # ✅ Allow cookies & auth headers  
    origins=ALLOWED_CORS_ORIGINS,  # ✅ Uses correct list dynamically  
    methods=["GET", "POST", "OPTIONS"],  # ✅ Restrict allowed HTTP methods  
    allow_headers=["Content-Type", "Authorization"],  # ✅ Allow required headers  
    expose_headers=["Set-Cookie"],  # ✅ Allows cookies in responses
    credentials=True
)  

print(f"✅ CORS Configured for: {ALLOWED_CORS_ORIGINS}")

# ---------- Initialize the Websocket SocketIO instance ----------#

# ✅ Dynamically Set CORS Allowed Origins
socketio = SocketIO(
    app,
    cors_allowed_origins=ALLOWED_CORS_ORIGINS,  # ✅ Uses same list dynamically 
    transports=["websocket"],  # ✅ Force WebSockets (no polling)
    ping_interval=25,  # ✅ Helps keep connection alive
    ping_timeout=60  # ✅ Prevents WebSocket from closing too soon
)

print(f"✅ WebSockets Configured for: {ALLOWED_CORS_ORIGINS}")

# ------------------------------------------------------------------------------


# ------------------------------------------------------------------------------
# Limiter
# ------------------------------------------------------------------------------
limiter = Limiter(
    key_func=get_remote_address
)
limiter.init_app(app)

#-------------------------  Custom Jinja Filter  ------------------------------#
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%Y-%m-%d %H:%M:%S'):
    if not value:
        return "N/A"
    return datetime.fromisoformat(value).strftime(format)


# ------------------------------------------------------------------------------
# Configure Stripe
# ------------------------------------------------------------------------------

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')


# ------------------------------------------------------------------------------
# Brevo API Key
# ------------------------------------------------------------------------------

BREVO_API_KEY = os.getenv('BREVO_API_KEY')
BREVO_SEND_EMAIL_URL = "https://api.brevo.com/v3/smtp/email"

# ------------------------------------------------------------------------------
# QuickBooks OAuth config
# ------------------------------------------------------------------------------
if DEV_MODE:
    CLIENT_ID = os.getenv('QB_SANDBOX_CLIENT_ID')
    CLIENT_SECRET = os.getenv('QB_SANDBOX_CLIENT_SECRET')
    REDIRECT_URI = os.getenv('SANDBOX_REDIRECT_URI')
    QUICKBOOKS_API_BASE_URL = "https://sandbox-quickbooks.api.intuit.com/v3/company/"
    REVOKE_TOKEN_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
    LOGGING_LEVEL = 'DEBUG'
    logging.info("Using Sandbox QuickBooks credentials.")
else:
    CLIENT_ID = os.getenv('QB_PROD_CLIENT_ID')
    CLIENT_SECRET = os.getenv('QB_PROD_CLIENT_SECRET')
    REDIRECT_URI = os.getenv('PROD_REDIRECT_URI')
    QUICKBOOKS_API_BASE_URL = "https://quickbooks.api.intuit.com/v3/company/"
    REVOKE_TOKEN_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
    LOGGING_LEVEL = 'INFO'
    logging.info("Using Production QuickBooks credentials.")

AUTHORIZATION_BASE_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
SCOPE = "com.intuit.quickbooks.accounting"

logging.info(f"Using REDIRECT_URI: {REDIRECT_URI}")
assert REDIRECT_URI in [
    "http://localhost:5000/callback",
    "https://app.linkbooksai.com/callback",
], f"Mismatch in REDIRECT_URI configuration. Current: {REDIRECT_URI}"

# ------------------------------------------------------------------------------
# OpenAI client
# ------------------------------------------------------------------------------
openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
debug_mode = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

# ------------------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------------------

#--------------Brevo-----------------#

# Flask-Mail Configuration
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS') == 'True'
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = (
    os.getenv('MAIL_DEFAULT_SENDER_NAME'),
    os.getenv('MAIL_DEFAULT_SENDER_EMAIL')
)

mail = Mail(app)


def send_verification_email(email, token):
    try:
        verification_link = f"https://linkbooksai.com/verify-email?token={token}"
        msg = Message(
            subject="Verify Your Email Address",
            recipients=[email],
            html=f"""
                <html>
                    <body>
                        <p>Hello,</p>
                        <p>Thank you for subscribing to LinkBooksAI!</p>
                        <p>Please verify your email address by clicking the link below:</p>
                        <a href="{verification_link}">Verify Email</a>
                        <p>This link will expire in 24 hours.</p>
                        <p>If you did not subscribe, please ignore this email.</p>
                    </body>
                </html>
            """
        )
        mail.send(msg)
        logging.info(f"Verification email sent to {email}.")
    except Exception as e:
        logging.error(f"Failed to send email to {email}: {e}")
        raise Exception("Email sending failed.")



def create_user_with_email(user_data):
    """
    Creates a user in Supabase Auth and user_profiles.
    Raises an exception if any step fails.
    Returns the user_id on success.
    """
    email = user_data.get("email")
    password = user_data.get("password")
    name = user_data.get("name")
    phone = user_data.get("phone")
    address = user_data.get("address")

    # Step 1: Create the user in Supabase Auth
    try:
        auth_response = supabase.auth.sign_up({"email": email, "password": password})
        if auth_response.get('user'):
            user_id = auth_response['user']['id']
            logging.info(f"User {name} created successfully in Supabase Auth with ID: {user_id}")
        else:
            logging.error(f"Error creating user in Supabase Auth: {auth_response}")
            raise Exception("Failed to create user in Supabase Auth.")
    except Exception as e:
        logging.error(f"Error creating user in Supabase Auth: {e}")
        raise Exception("Failed to create user.")

    # Step 2: Insert additional user data
    try:
        profile_response = supabase.table('user_profiles').insert({
            'id': user_id,
            'name': name,
            'email': email,
            'phone': phone,
            'address': address,
            'subscription_status': 'pending',  # Set to 'pending' initially
        }).execute()

        if profile_response.data:
            logging.info(f"User profile created successfully for {name}.")
        else:
            logging.error(f"Error creating user profile: {profile_response}")
            raise Exception("Failed to create user profile.")
    except Exception as e:
        logging.error(f"Error creating user profile: {e}")
        # Rollback user creation in Supabase Auth
        try:
            supabase.auth.api.delete_user(user_id)
            logging.info(f"Deleted user {user_id} due to profile creation failure.")
        except Exception as delete_error:
            logging.error(f"Error deleting user {user_id}: {delete_error}")
        raise Exception("Failed to create user profile.")

    return user_id

# ✅ Debug Logging Function (Ensure this is before generate())
def log_debug(msg):
    print(f"{datetime.now().isoformat()} - {msg}", flush=True)

def generate_session_token(user_id, email):
    """
    Generates a JWT token with 24-hour expiry.
    """
    token = jwt.encode(
        {
            "user_id": user_id,
            "email": email,
            "exp": datetime.now(timezone.utc) + timedelta(hours=24)
        },
        SECRET_KEY,
        algorithm="HS256"
    )
    return token

def token_required(f):
    """
    Decorator requiring a valid token from the session_token cookie.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get("session_token")
        if not token:
            return {"error": "No Authorization token provided"}, 401
        try:
            decoded = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            request.user_id = decoded.get("user_id")
            if not request.user_id:
                raise Exception("No user_id found in the token.")
        except jwt.ExpiredSignatureError:
            return {"error": "Token has expired"}, 401
        except jwt.InvalidTokenError:
            return {"error": "Invalid token"}, 401
        except Exception:
            return {"error": "Unauthorized access. Please log in again."}, 401

        return f(*args, **kwargs)
    return decorated

def get_quickbooks_tokens(user_id):
    """
    Retrieve QuickBooks tokens for a given user ID from quickbooks_tokens table.
    """
    try:
        response = supabase.table("quickbooks_tokens").select("*").eq("user_id", user_id).execute()
        if not response.data: # No tokens found
            logging.error(f"No QuickBooks tokens found for user {user_id}.")
            raise Exception("No QuickBooks tokens found for the user.")
        return response.data[0]
    except Exception as e:
        logging.error(f"Error fetching QuickBooks tokens: {e}")
        raise Exception("Failed to fetch QuickBooks tokens.")

def save_quickbooks_tokens(user_id, realm_id, access_token, refresh_token, token_expiry):
    """
    Upserts QuickBooks tokens for the single user row identified by user_id.
    This ensures only ONE row per user in quickbooks_tokens.
    """
    try:
        # upsert with a dictionary where user_id is the same each time
        response = supabase.table("quickbooks_tokens") \
            .upsert({
                "user_id": user_id,
                "realm_id": realm_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expiry": token_expiry,
                "last_updated": datetime.utcnow().isoformat()
            }) \
            .execute()

        # Optionally check response.data or response.error
        logging.info(f"QuickBooks tokens saved successfully for user_id={user_id}.")
    except Exception as e:
        logging.error(f"Error saving QuickBooks tokens for user_id={user_id}: {e}")
        raise Exception("Failed to save QuickBooks tokens.")


def refresh_access_token(user_id):
    """
    Refreshes the QuickBooks access token for an app-based user (one row per user).
    """
    quickbooks_data = get_quickbooks_tokens(user_id)
    if not quickbooks_data:
        raise Exception("No QuickBooks tokens found for this user.")

    refresh_token = quickbooks_data['refresh_token']
    auth_header = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
    payload = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
    headers = {'Accept': 'application/json'}

    response = requests.post(TOKEN_URL, auth=auth_header, data=payload, headers=headers)
    if response.status_code == 200:
        tokens_json = response.json()
        new_access_token = tokens_json['access_token']
        new_refresh_token = tokens_json.get('refresh_token', refresh_token)
        new_expiry = (datetime.utcnow() + timedelta(seconds=tokens_json['expires_in'])).isoformat()

        save_quickbooks_tokens(
            user_id=user_id,
            realm_id=quickbooks_data['realm_id'],
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            token_expiry=new_expiry
        )
        logging.info(f"Access token refreshed for user {user_id}.")
    else:
        logging.error(f"Failed to refresh access token for user_id={user_id}: {response.text}")
        raise Exception(response.text)


def get_company_info(user_id):
    """
    Fetches the QuickBooks company info for the specified user_id.
    Automatically refreshes if tokens are expired.
    """
    try:
        tokens = get_quickbooks_tokens(user_id)
        access_token = tokens.get('access_token')
        realm_id = tokens.get('realm_id')
        expiry = tokens.get('token_expiry')

        if not access_token:
            raise Exception("No access token found. QuickBooks disconnected.")

        # Check for expiry
        if expiry and datetime.utcnow() > datetime.fromisoformat(expiry):
            logging.info("Access token expired. Refreshing...")
            refresh_access_token(user_id)
            tokens = get_quickbooks_tokens(user_id)
            access_token = tokens.get('access_token')
            realm_id = tokens.get('realm_id')

        # Make request to QBO
        headers = {
            'Authorization': f"Bearer {access_token}",
            'Accept': 'application/json'
        }
        api_url = f"{QUICKBOOKS_API_BASE_URL}{realm_id}/companyinfo/{realm_id}"
        response = requests.get(api_url, headers=headers)

        if response.status_code == 401:
            logging.info("Got 401 fetching company info. Trying refresh again...")
            refresh_access_token(user_id)
            tokens = get_quickbooks_tokens(user_id)
            access_token = tokens.get('access_token')
            headers['Authorization'] = f"Bearer {access_token}"
            response = requests.get(api_url, headers=headers)

        if response.status_code == 200:
            return response.json().get("CompanyInfo", {})
        else:
            logging.error(f"QuickBooks API Error: {response.status_code} - {response.text}")
            raise Exception(f"Failed to fetch company info: {response.status_code} {response.text}")
    except Exception as e:
        logging.error(f"Error in get_company_info: {e}")
        raise

def store_tokens_for_chatgpt_session(chat_session_id, realm_id, access_token, refresh_token, expiry):
    """
    Stores QuickBooks tokens associated with a ChatGPT session ID into 'chatgpt_tokens'.
    """
    try:
        # Retrieve user_id from user_profiles
        user_profile = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()
        if not user_profile.data:
            logging.error(f"No user found for chatSessionId: {chat_session_id}")
            raise ValueError("User not found for given chatSessionId")

        user_id = user_profile.data[0]["id"]

        payload = {
            "chat_session_id": chat_session_id,
            "user_id": user_id,
            "realm_id": realm_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expiry": expiry
        }
        logging.info(f"Payload for chatgpt_tokens upsert: {payload}")

        token_response = supabase.table("chatgpt_tokens").upsert(payload).execute()

        if not token_response.data:  # Ensure response contains data
            logging.error(f"Failed to store tokens. Response: {token_response}")
            raise ValueError("Failed to store tokens")



        logging.info(f"Tokens stored successfully for chatSessionId: {chat_session_id}")
    except Exception as e:
        logging.error(f"Error storing tokens for ChatGPT session {chat_session_id}: {e}")
        raise

def generate_random_state(length=16):
    """
    Generates a random `state` string for OAuth (CSRF protection).
    """
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def revoke_quickbooks_tokens(refresh_token):
    """
    Revokes the given refresh token with QuickBooks.
    """
    try:
        auth_header = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
        payload = {'token': refresh_token}
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        response = requests.post(REVOKE_TOKEN_URL, auth=auth_header, data=payload, headers=headers)
        if response.status_code != 200:
            raise Exception(f"Failed to revoke tokens: {response.text}")
        logging.info("QuickBooks tokens revoked successfully.")
    except Exception as e:
        logging.error(f"Error revoking tokens: {e}")
        raise
    


# ------------------------------------------------------------------------------
# Optional store_state() - if used anywhere, ensure it supplies a non-null 'state'
# ------------------------------------------------------------------------------
"""
def store_state(chat_session_id, state):
    expiry = datetime.utcnow() + timedelta(minutes=30)
    try:
        supabase.table("chatgpt_oauth_states").upsert({
            "state": state,
            "chat_session_id": chat_session_id,
            "expiry": expiry.isoformat()
        }).execute()
        logging.info(f"Stored state: {state} with expiry: {expiry}")
    except Exception as e:
        logging.error(f"Failed to store state: {state}, Error: {e}")
        raise
"""

def validate_state(state):
    """
    Validates the incoming state against chatgpt_oauth_states (CSRF protection).
    """
    response = supabase.table("chatgpt_oauth_states").select("*").eq("state", state).execute()
    if not response.data:
        logging.error(f"State not found in database: {state}")
        raise ValueError("Invalid or expired state parameter.")

    stored_state = response.data[0]
    expiry = datetime.fromisoformat(stored_state["expiry"])
    if datetime.utcnow() > expiry:
        logging.error(f"State expired. Generated: {stored_state['expiry']} Current: {datetime.utcnow()}")
        raise ValueError("State token expired.")
    return stored_state

    
#--------------------------------------------------
#                 Subscription Helper/Defs        #
#--------------------------------------------------

def create_stripe_checkout_session(user_id, email, subscription_plan, chat_session_id=None):
    """
    Creates and returns a Stripe Checkout Session 
    for the given user and subscription plan.
    """
    # Map subscription plans to Stripe Price IDs and trial durations
    plan_details = {
        "monthly_no_offer": {"price_id": "price_1QhXfxDi1nqWbBYc76q14cWL", "trial_days": 0},
        "monthly_3mo_discount": {"price_id": "price_1QhdvrDi1nqWbBYcWOcfXTRJ", "trial_days": 0},
        "annual_free_week": {"price_id": "price_1QhdyFDi1nqWbBYcdzAdZ7lE", "trial_days": 7},
        "annual_further_discount": {"price_id": "price_1Qhe01Di1nqWbBYcixjWCokH", "trial_days": 0}
    }

    # Validate the selected plan
    plan = plan_details.get(subscription_plan)
    if not plan:
        raise ValueError("Invalid subscription plan selected")

    # Extract price ID and trial period for the selected plan
    price_id = plan["price_id"]
    trial_period_days = plan["trial_days"]

    # Build success and cancel URLs with optional chat_session_id
    base_success_url = "https://linkbooksai.com/payment_success"
    base_cancel_url = "https://linkbooksai.com/payment_cancel"

    success_url = f"{base_success_url}?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = base_cancel_url

    if chat_session_id:
        success_url += f"&chat_session_id={chat_session_id}"
        cancel_url += f"?chat_session_id={chat_session_id}"

    try:
        # Prepare subscription data
        subscription_data = {}
        if trial_period_days > 0:
            subscription_data["trial_period_days"] = trial_period_days  # Include trial only for eligible plans

        # Create the Stripe Checkout Session
        stripe_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            customer_email=email,
            subscription_data=subscription_data,  # Add trial days only if > 0
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "subscription_plan": subscription_plan,
                "chat_session_id": chat_session_id,
                "user_id": user_id  # Include user_id for webhook association
            }
        )
        return stripe_session  # Return the full stripe session object
    
    except stripe.error.StripeError as e:
        logging.error(f"Stripe API error: {e}")
        raise Exception(f"Failed to create Stripe session: {str(e)}")


    # -------------------------
    # App Requests functions/definitions
    # -------------------------#

def fetch_report(
    user_id: str,
    report_type: str,
    start_date: str = None,
    end_date: str = None
) -> dict:
    """
    Fetches a financial report from QuickBooks for the specified user_id.
    This function:
      1) Retrieves the user’s QBO tokens from Supabase,
      2) Checks for expiry, refreshes if needed,
      3) Calls the QuickBooks /reports endpoint,
      4) Returns the JSON response or raises an Exception.
    """
    # 1) Retrieve tokens from DB
    tokens = get_quickbooks_tokens(user_id)
    access_token = tokens.get("access_token")
    realm_id     = tokens.get("realm_id")
    expiry_str   = tokens.get("token_expiry")

    if not access_token or not realm_id:
        raise Exception("Missing QuickBooks tokens or realm_id for this user.")

    # 2) Check if expired
    if expiry_str:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if datetime.utcnow() > expiry_dt:
            logging.info("Access token expired; refreshing tokens...")
            refresh_access_token(user_id)
            # Now refetch the updated tokens
            tokens = get_quickbooks_tokens(user_id)
            access_token = tokens["access_token"]
            realm_id     = tokens["realm_id"]

    # 3) Make the request to QuickBooks
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/reports/{report_type}"

    params = {}
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    response = requests.get(base_url, headers=headers, params=params)

    # 4) Handle possible 401 mid-request (token invalid again)
    if response.status_code == 401:
        logging.info("Token might have expired mid-request. Attempting second refresh...")
        refresh_access_token(user_id)
        tokens = get_quickbooks_tokens(user_id)
        access_token = tokens["access_token"]
        headers["Authorization"] = f"Bearer {access_token}"
        response = requests.get(base_url, headers=headers, params=params)

    # 5) Final check
    if response.status_code == 200:
        return response.json()
    else:
        logging.error(f"Error fetching {report_type} report: {response.text}")
        raise Exception(f"Failed to fetch {report_type} report: {response.status_code} {response.text}")

#--------------- Fetch Transactions Def -------------------#

def filter_transactions_locally(transactions: list, local_filters: dict) -> list:
    """
    Filters a list of transaction dictionaries based on provided filter criteria.
    Performs a case-insensitive substring search on the relevant fields.
    
    :param transactions: List of transaction dicts.
    :param local_filters: Dictionary of filter keys and values (e.g., {"vendor": "McDonald's"}).
                          Note: For filtering by vendor, we assume the vendor name is stored in the "name" field.
    :return: Filtered list of transactions.
    """
    filtered = []
    for txn in transactions:
        include = True
        for key, filter_value in local_filters.items():
            # Map "vendor" to "name" since that's where the vendor name appears in the report.
            field = "name" if key == "vendor" else key
            txn_value = txn.get(field, "")
            if filter_value.lower() not in txn_value.lower():
                include = False
                break
        if include:
            filtered.append(txn)
    return filtered


def fetch_transactions(user_id: str, qb_params: dict) -> dict:
    """
    Fetches the TransactionList report from QuickBooks using a dynamic set of query parameters,
    then applies local filtering for parameters that require reference IDs (e.g. vendor, customer).
    
    The function:
      1. Retrieves QuickBooks tokens (and refreshes them if expired).
      2. Separates out parameters that QuickBooks expects as IDs (e.g. vendor) so that free‑form text isn’t sent.
      3. Calls the TransactionList endpoint.
      4. Processes the returned JSON—handling grouped rows and ignoring summary rows.
      5. Applies local filtering based on the removed parameters.
    
    :param user_id: The QuickBooks-connected user ID.
    :param qb_params: Dictionary of query parameters from the request.
    :return: A dictionary with a "transactions" key containing the filtered list.
    """
    # 1) Retrieve tokens from Supabase.
    tokens = get_quickbooks_tokens(user_id)
    access_token = tokens.get("access_token")
    realm_id = tokens.get("realm_id")
    expiry_str = tokens.get("token_expiry")
    
    if not access_token or not realm_id:
        raise Exception("Missing QuickBooks tokens or realm_id for this user.")
    
    # 2) Refresh token if expired.
    if expiry_str:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if datetime.utcnow() > expiry_dt:
            logging.info("Access token expired; refreshing tokens...")
            refresh_access_token(user_id)
            tokens = get_quickbooks_tokens(user_id)
            access_token = tokens.get("access_token")
            realm_id = tokens.get("realm_id")
    
    # 3) Separate out filters that require local handling.
    # These keys (like vendor, customer, name, department, memo) expect reference IDs in QB,
    # so remove them from qb_params and store them for local filtering.
    local_filter_keys = ["vendor", "customer", "name", "department", "memo"]
    local_filters = {}
    for key in local_filter_keys:
        if key in qb_params:
            local_filters[key] = qb_params.pop(key)
    
    # 4) Build the API request to QuickBooks.
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/reports/TransactionList"
    response = requests.get(base_url, headers=headers, params=qb_params)
    
    # 5) Handle potential token expiry mid-request.
    if response.status_code == 401:
        logging.info("Token might have expired mid-request. Attempting second refresh...")
        refresh_access_token(user_id)
        tokens = get_quickbooks_tokens(user_id)
        access_token = tokens.get("access_token")
        headers["Authorization"] = f"Bearer {access_token}"
        response = requests.get(base_url, headers=headers, params=qb_params)
    
    if response.status_code != 200:
        logging.error(f"Error fetching TransactionList report: {response.text}")
        raise Exception(f"Failed to fetch TransactionList report: {response.status_code} {response.text}")
    
    report_data = response.json()
    
    # 6) Process the JSON response to extract transaction rows.
    transactions = []
    rows = report_data.get("Rows", {}).get("Row", [])
    
    for row in rows:
        # If the row is a grouping (contains nested Rows), process each nested row.
        if "Rows" in row and "Row" in row["Rows"]:
            for data_row in row["Rows"]["Row"]:
                # Only process rows with type "Data" (ignore any other types).
                if data_row.get("type") != "Data":
                    continue
                col_data = data_row.get("ColData", [])
                # Ensure there are at least 10 columns as expected.
                if len(col_data) < 10:
                    continue
                txn = {
                    "date": col_data[0].get("value", ""),
                    "transaction_type": col_data[1].get("value", ""),
                    "doc_num": col_data[2].get("value", ""),
                    "posting": col_data[3].get("value", ""),
                    "name": col_data[4].get("value", ""),
                    "department": col_data[5].get("value", ""),
                    "memo": col_data[6].get("value", ""),
                    "account": col_data[7].get("value", ""),
                    "split": col_data[8].get("value", ""),
                    "amount": col_data[9].get("value", "")
                }
                transactions.append(txn)
        # If the row is standalone, process it directly.
        else:
            # Optionally, check if row type is "Data".
            if row.get("type") != "Data":
                continue
            col_data = row.get("ColData", [])
            if len(col_data) < 10:
                continue
            txn = {
                "date": col_data[0].get("value", ""),
                "transaction_type": col_data[1].get("value", ""),
                "doc_num": col_data[2].get("value", ""),
                "posting": col_data[3].get("value", ""),
                "name": col_data[4].get("value", ""),
                "department": col_data[5].get("value", ""),
                "memo": col_data[6].get("value", ""),
                "account": col_data[7].get("value", ""),
                "split": col_data[8].get("value", ""),
                "amount": col_data[9].get("value", "")
            }
            transactions.append(txn)
    
    # 7) Apply local filtering if any filters were provided.
    if local_filters:
        transactions = filter_transactions_locally(transactions, local_filters)
    
    return {"transactions": transactions}


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

# ✅ Serve Static Assets for Svelte
@app.route('/assets/<path:path>')
def serve_static_assets(path):
    return send_from_directory(os.path.join(app.static_folder, "assets"), path)

# ✅ Serve Svelte frontend files
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_svelte_frontend(path):
    # ✅ If the request is for an API, don’t interfere
    if path.startswith("api/"):
        abort(404)  # Stops Flask from hijacking API calls

    # ✅ If the request is for a Flask template, return the Flask page
    flask_pages = ["login", "dashboard"]
    if path in flask_pages:
        return render_template(f"{path}.html")

    # ✅ Otherwise, serve the Svelte frontend
    return send_from_directory(app.static_folder, "index.html")


# ------------------------------------------
# Login Route
# ------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute", error_message="Too many login attempts. Please try again in a minute.")
def login():
    if request.method == 'GET':
        chat_session_id = request.args.get('chatSessionId')
        return render_template('login.html', chatSessionId=chat_session_id)

    try:
        data = request.form
        email = data.get('email', '').strip().lower()
        password = data.get('password')
        chat_session_id = data.get('chatSessionId')

        if not email or not password:
            error_message = "Email and password are required."
            return render_template('login.html', error_message=error_message, chatSessionId=chat_session_id), 400

        # Check user in 'users' table
        response = supabase.table("users").select("id").eq("email", email).execute()
        if not response.data:  # No user found
            error_message = "No account found with that email."
            logging.warning(f"Login failed: No account found for email {email}.")
            return render_template('login.html', error_message=error_message, chatSessionId=chat_session_id), 401

        user_id = response.data[0]["id"]

        # Attempt sign_in with supabase.auth
        try:
            auth_response = supabase.auth.sign_in_with_password({"email": email, "password": password})
            logging.info(f"Auth response: {auth_response}")
        except AuthApiError as e:
            error_msg = str(e).lower()
            if 'invalid login credentials' in error_msg or 'invalid password' in error_msg:
                error_message = "Invalid email or password."
            elif 'too many requests' in error_msg or 'rate limit' in error_msg:
                error_message = "Too many login attempts. Please try again later."
            elif 'jwt expired' in error_msg:
                error_message = "Session expired. Please log in again."
            else:
                error_message = "An error occurred during login. Please try again."
            return render_template('login.html', error_message=error_message, chatSessionId=chat_session_id), 401

        # Generate session token
        token = generate_session_token(user_id, email)
        logging.info(f"Generated session token for user ID: {user_id}")
        
         # Store the user's ID and email in the session for later use
        session['user_id'] = user_id
        session['email'] = email

        # Link ChatGPT session ID if provided
        if chat_session_id:
            try:
                link_response = supabase.table("user_profiles").update({
                    "chat_session_id": chat_session_id
                }).eq("id", user_id).execute()

                # Check for errors in the response
                if not link_response.data:
                    logging.warning(
                        f"Failed to link chatSessionId for user {user_id}. "
                        f"Error: {link_response.error}"
                    )
                else:
                    logging.info(f"Successfully linked chatSessionId {chat_session_id} to user {user_id}.")
            except Exception as e:
                logging.error(f"Error linking chatSessionId for user {user_id}: {e}")

        resp = make_response(
            redirect(
                url_for('dashboard') if not chat_session_id
                else url_for('link_chat_session', chatSessionId=chat_session_id)
            )
        )
        secure_cookie = app.config.get("ENV") == "production"
        resp.set_cookie(
            "session_token",
            token,
            httponly=True,
            secure=True, # ✅ Ensures the cookie is sent only over HTTPS
            samesite="None",  # ✅ Allows sharing across different subdomains
            domain=".linkbooksai.com"  # ✅ Makes the cookie work across all subdomains
        )
        logging.info(f"Session token set for user ID: {user_id}")
        return resp

    except Exception as e:
        logging.error(f"Error during login: {e}", exc_info=True)
        error_message = "An unexpected error occurred during login. Please try again."
        return render_template('login.html', error_message=error_message), 500

# -------------For svelte to check login status-----------------#
@app.route("/auth/status", methods=["GET"])
def check_auth_status():
    session_token = request.cookies.get("session_token")
    
    print(f"🔍 Checking auth status... Cookies received: {request.cookies}")  # Debugging log

    if not session_token:
        print("❌ No session token found! Redirecting user.")
        return jsonify({"logged_in": False, "message": "No session token found"}), 401

    return jsonify({"logged_in": True, "session_token": session_token})


# ------------------------------------------
# Create Account Routes
# ------------------------------------------
@app.route('/create-account', methods=['GET', 'POST'])
def create_account():
    if request.method == 'GET':
        # Render the Create Account page, passing chat_session_id
        chat_session_id = request.args.get('chat_session_id', None)
        return render_template('create_account.html', chat_session_id=chat_session_id)

    elif request.method == 'POST':
        # Handle form submission
        data = request.form
        chat_session_id = data.get('chat_session_id', None)  # Preserve chat_session_id
        name = data.get('name', '').strip()
        email = data.get('email', '').strip().lower()
        password = data.get('password')
        confirm_password = data.get('confirm_password')
        phone = data.get('phone', '').strip()
        address = data.get('address', '').strip()

        if not email or not password or not confirm_password:
            return jsonify({"success": False, "error_message": "Email and passwords are required."}), 400

        if password != confirm_password:
            return jsonify({"success": False, "error_message": "Passwords do not match."}), 400

        if len(password) < 6:
            return jsonify({"success": False, "error_message": "Password must be at least 6 characters long."}), 400

        # Check if user already exists
        try:
            response = supabase.table("users").select("id").eq("email", email).execute()
            if response.data:
                return jsonify({"success": False, "error_message": "An account with this email already exists."}), 400
        except Exception as e:
            logging.error(f"Error checking for existing account: {e}, chat_session_id: {chat_session_id}")
            return jsonify({"success": False, "error_message": "Failed to check for an existing account."}), 500

        # Create user in Supabase Auth
        try:
            auth_response = supabase.auth.sign_up({"email": email, "password": password})
            user_id = auth_response.user.id if auth_response.user else None
            if not user_id:
                raise Exception("Failed to create user in Supabase Auth.")
        except Exception as e:
            logging.error(f"Auth creation failed: {e}, chat_session_id: {chat_session_id}")
            return jsonify({"success": False, "error_message": "Error creating account."}), 500

        # Insert user profile
        try:
            user_profile = {
                "id": user_id,
                "name": name,
                "phone": phone,
                "address": address,
                'subscription_status': 'inactive',  # Set to 'inactive' initially
                "gpt_config": {"default_behavior": "friendly"},
                "is_verified": False,
            }
            supabase.table("user_profiles").insert(user_profile).execute()
        except Exception as e:
            logging.error(f"Error inserting user profile: {e}, chat_session_id: {chat_session_id}")
            # Rollback: Delete the user from Supabase Auth to prevent orphaned Auth users
            try:
                supabase.auth.api.delete_user(user_id)
                logging.info(f"Deleted user {user_id} due to profile creation failure.")
            except Exception as delete_error:
                logging.error(f"Error deleting user {user_id}: {delete_error}")
            return jsonify({"success": False, "error_message": "Failed to save user profile."}), 500


        # Save user details in the session to auto-login
        session['user_id'] = user_id
        session['email'] = email
        if chat_session_id:
            session['chat_session_id'] = chat_session_id

        return redirect(url_for('subscriptions'))




#--------------------------------------------#
#              Subscriptions                 #
#--------------------------------------------#

@app.route('/subscriptions', methods=['GET', 'POST'])
def subscriptions():
    if request.method == 'GET':
        email = session.get('email')
        chat_session_id = session.get('chat_session_id', None)
        user_id = session.get('user_id')  # Ensure user_id is passed

        if not email:
            return redirect(url_for('create_account'))

        user_profile = supabase.table("user_profiles").select("subscription_status").eq("email", email).execute()
        if user_profile.data:
            subscription_status = user_profile.data[0].get("subscription_status")
            if subscription_status == "active":
                return redirect(url_for('dashboard'))
            elif subscription_status == "pending":
                return render_template('subscriptions.html', email=email, chat_session_id=chat_session_id, user_id=user_id, message="Your payment is being processed. Please check your email for verification.")
            elif subscription_status == "inactive":
                pass
        else:
            return redirect(url_for('create_account'))

        return render_template('subscriptions.html', email=email, chat_session_id=chat_session_id, user_id=user_id)
    
    elif request.method == 'POST':
        # Receive JSON data from front-end
        data = request.json
        email = data.get('email')
        subscription_plan = data.get('subscription_plan')
        chat_session_id = data.get('chat_session_id', None)

        if not email or not subscription_plan:
            return jsonify({'error': 'Email and subscription plan are required'}), 400

        # Look up user_id by email
        user_profile = supabase.table("user_profiles").select("id").eq("email", email).execute()
        if not user_profile.data:
            return jsonify({'error': 'No user found with that email'}), 404

        user_id = user_profile.data[0]['id']
        subscription_status = user_profile.data[0].get('subscription_status')

        if subscription_status == "active":
            return jsonify({'error': 'You already have an active subscription.'}), 400
        elif subscription_status == "pending":
            return jsonify({'error': 'Your payment is being processed. Please wait or contact support.'}), 400


        try:
            # Use your helper function to create the Stripe session
            stripe_session = create_stripe_checkout_session(
                user_id=user_id,
                email=email,
                subscription_plan=subscription_plan,
                chat_session_id=chat_session_id
            )

            # Return the 'url' property so the front-end can redirect to Stripe Checkout
            return jsonify({'checkoutUrl': stripe_session.url}), 200

        except Exception as e:
            logging.error(f"Stripe session creation failed: {e}, chat_session_id: chat_session_id")
            return jsonify({'error': str(e)}), 500


#------------------------------------------------#    
#                  Stripe Routes                 #
#------------------------------------------------#

@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():

    logging.error("Received a POST request to /stripe-webhook")
    logging.error(f"Headers: {request.headers}")
    logging.error(f"Body: {request.get_data(as_text=True)}")

    payload = request.get_data(as_text=True)
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.getenv('STRIPE_WEBHOOK_SECRET')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
        logging.error(f"Successfully constructed Stripe event: {event['type']}")
    except ValueError:
        logging.error("Invalid payload")
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        logging.error("Invalid signature")
        return "Invalid signature", 400

    event_type = event["type"]
    data = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            handle_checkout_session_completed(data)
        elif event_type == "invoice.payment_succeeded":
            handle_invoice_payment_succeeded(data)
        elif event_type == "customer.subscription.updated":
            handle_customer_subscription_updated(data)
        else:
            logging.info(f"Unhandled event type: {event_type}")
    except Exception as e:
        logging.error(f"Error processing event {event_type}: {e}", exc_info=True)
        return "Error processing event", 500

    return "", 200

def handle_checkout_session_completed(session):
    email = session.get("customer_email")
    subscription_plan = session.get("metadata", {}).get("subscription_plan")
    chat_session_id = session.get("metadata", {}).get("chat_session_id")
    customer_id = session.get("customer")
    user_id = session.get("metadata", {}).get("user_id")
    subscription_id = session.get("subscription")
    free_week = True if subscription_plan == "annual_free_week" else False

    if not user_id:
        raise Exception("No user_id found in session metadata.")

    # Update user's subscription status and preserve chat_session_id
    supabase.table("user_profiles").upsert({
        "id": user_id,
        "email": email,
        "subscription_status": "active",
        "subscription_plan": subscription_plan,
        "subscription_id": subscription_id,
        "chat_session_id": chat_session_id,
        "customer_id": customer_id,
        "free_week": free_week,  # Set free_week based on the plan
        "updated_at": datetime.utcnow().isoformat()
    }).execute()

    # Generate verification token
    token = secrets.token_urlsafe(32)
    expiry = datetime.utcnow() + timedelta(hours=24)

    # Store verification token
    supabase.table("email_verifications").insert({
        "user_id": user_id,
        "token": token,
        "expires_at": expiry.isoformat(),
        "created_at": datetime.utcnow().isoformat()
    }).execute()

    # Attempt to send the verification email via Brevo
    try:
        send_verification_email(email, token)
    except Exception as e:
        # Log the error but do not raise an exception so that we return a 200 to Stripe.
        logging.error(f"Failed to send verification email to {email}: {e}")
        # Optionally, you might want to notify yourself or take other action here.


def handle_checkout_session_failed(session):
    """
    Handle failed checkout sessions.
    """
    email = session.get("customer_email")
    user_id = session.get("metadata", {}).get("user_id")
    chat_session_id = session.get("metadata", {}).get("chat_session_id")

    if not user_id:
        logging.error("No user_id found in session metadata for failed payment.")
        return

    # Option A: Delete the user if not verified
    user_profile = supabase.table("user_profiles").select("is_verified").eq("id", user_id).execute()
    if user_profile.data and not user_profile.data[0].get("is_verified"):
        try:
            supabase.auth.api.delete_user(user_id)
            supabase.table("user_profiles").delete().eq("id", user_id).execute()
            logging.info(f"Deleted unverified user {user_id} due to failed payment.")
        except Exception as e:
            logging.error(f"Error deleting user {user_id}: {e}")
    else:
        # Option B: Update subscription_status to 'inactive'
        try:
            supabase.table("user_profiles").update({
                "subscription_status": "inactive",
                "updated_at": datetime.utcnow().isoformat()
            }).eq("id", user_id).execute()
            logging.info(f"Set subscription_status to 'inactive' for user {user_id} due to failed payment.")
        except Exception as e:
            logging.error(f"Error updating subscription status for user {user_id}: {e}")

def handle_invoice_payment_succeeded(invoice):
    customer_id = invoice['customer']

    user_profile = supabase.table("user_profiles").select("*").eq("customer_id", customer_id).execute()
    if not user_profile.data:
        logging.error(f"No user found for customer_id {customer_id}.")
        raise Exception("User not found")

    supabase.table("user_profiles").update({"subscription_status": "active"}).eq("customer_id", customer_id).execute()

def handle_customer_subscription_updated(subscription):
    customer_id = subscription["customer"]
    # Here, subscription["id"] is the subscription id
    subscription_id = subscription.get("id")
    status = subscription["status"]
    trial_end = subscription.get("trial_end")

    updates = {"subscription_status": status,
               "subscription_id": subscription_id  # Update subscription_id here
    }
    if trial_end and trial_end < datetime.utcnow().timestamp():
        updates["free_week"] = False

    user_profile = supabase.table("user_profiles").select("*").eq("customer_id", customer_id).execute()
    if not user_profile.data:
        logging.error(f"No user found for customer_id {customer_id}.")
        raise Exception("User not found")

    supabase.table("user_profiles").update(updates).eq("customer_id", customer_id).execute()


def create_subscription_schedule(customer_id):
    try:
        plan_details = {
            "monthly_no_offer": "price_1QhXfxDi1nqWbBYc76q14cWL",
            "monthly_3mo_discount": "price_1QhdvrDi1nqWbBYcWOcfXTRJ"
        }

        subscription_schedule = stripe.SubscriptionSchedule.create(
            customer=customer_id,
            start_date="now",  # Starts immediately
            end_behavior="release",  # Ensures it transitions to standard pricing
            phases=[
                {
                    "items": [{"price": plan_details["monthly_3mo_discount"], "quantity": 1}],
                    "iterations": 3,  # Lasts for 3 billing cycles
                    "cancellation_behavior": "none",  # Prevents early cancellation
                },
                {
                    "items": [{"price": plan_details["monthly_no_offer"], "quantity": 1}],
                    "iterations": None,  # Continues indefinitely
                },
            ],
        )
        return subscription_schedule
    except Exception as e:
        print(f"Error creating subscription schedule: {e}")
        return None


@app.route('/create-stripe-session', methods=['POST'])
@limiter.limit("10 per minute")
def create_stripe_session():
    try:
        logging.info(f"Received POST to /create-stripe-session: {request.json}")
        data = request.json  # Expecting JSON data
        user_id = data.get('user_id')
        email = data.get('email')
        subscription_plan = data.get('subscription_plan')
        chat_session_id = data.get('chat_session_id')  # Optional
        
        # Convert string "None" to actual None
        if chat_session_id == "None":
            chat_session_id = None
        if user_id == "None":
            user_id = None

        logging.info(f"Creating Stripe session for user: {email}, plan: {subscription_plan}")

        # Validate inputs
        if not user_id or not email or not subscription_plan:
            return jsonify({"success": False, "error_message": "Missing required fields."}), 400

        # Create Stripe Checkout Session
        stripe_session = create_stripe_checkout_session(
            user_id=user_id,
            email=email,
            subscription_plan=subscription_plan,
            chat_session_id=chat_session_id
        )

        logging.info(f"Stripe session created successfully: {stripe_session.id}")
        return jsonify({"success": True, "checkout_url": stripe_session.url}), 200

    except Exception as e:
        logging.error(f"Error in /create-stripe-session: {e}")
        return jsonify({"success": False, "error_message": "Failed to create Stripe session."}), 500




#-------------Stripe Redirect Routes-------------#

@app.route('/payment_success')
def payment_success():
    session_id = request.args.get('session_id')
    chat_session_id = request.args.get('chat_session_id')  # Optional for session continuity

    if not session_id:
        return "Missing session ID", 400

    try:
        # Optionally, retrieve session details for display
        session = stripe.checkout.Session.retrieve(session_id)
        customer_email = session.get("customer_email")

        # Render the payment success page
        return render_template(
            'payment_success.html',
            session_id=session_id,
            chat_session_id=chat_session_id,
            email=customer_email
        )

    except Exception as e:
        logging.error(f"Error in payment_success route: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/payment_cancel')
def payment_cancel():
    chat_session_id = request.args.get('chat_session_id')
    return render_template('payment_cancel.html', chat_session_id=chat_session_id)

#---------------Brevo Email Verify-------------------#

@app.route('/verify-email', methods=['GET'])
def verify_email():
    token = request.args.get('token')
    if not token:
        return render_template('verify_email.html', error="Invalid or missing token."), 400

    try:
        # Retrieve verification entry
        verification = supabase.table("email_verifications").select("*").eq("token", token).execute()
        if not verification.data:
            return render_template('verify_email.html', error="Invalid or expired token.", token=None), 400

        entry = verification.data[0]
        expiry = datetime.fromisoformat(entry['expires_at'])
        if datetime.utcnow() > expiry:
            return render_template('verify_email.html', error="Token has expired.", token=None), 400

        user_id = entry['user_id']

        # Check if user is already verified
        user_profile = supabase.table("user_profiles").select("is_verified").eq("id", user_id).execute()
        if user_profile.data and user_profile.data[0].get("is_verified"):
            logging.info(f"User {user_id} is already verified.")
            return render_template('verify_email.html', message="Your email is already verified."), 200

        # Activate and verify the user profile
        supabase.table("user_profiles").update({
            "is_verified": True
        }).eq("id", user_id).execute()

        # Retrieve chat_session_id if needed for further actions
        user_profile = supabase.table("user_profiles").select("chat_session_id").eq("id", user_id).execute()
        chat_session_id = user_profile.data[0].get("chat_session_id") if user_profile.data else None

        # Delete the verification token
        supabase.table("email_verifications").delete().eq("token", token).execute()

        # Optionally, redirect to a specific page based on chat_session_id
        if chat_session_id:
            return render_template('email_verified.html', chat_session_id=chat_session_id), 200
        else:
            return render_template('email_verified.html'), 200

    except Exception as e:
        logging.error(f"Error during email verification: {e}")
        return render_template('verify_email.html', error="An error occurred during verification. Please try again.", token=None), 500



# ------------------------------------------
# Protected Example: fetch-user-data
# ------------------------------------------
@app.route('/fetch-user-data', methods=['GET'])
@token_required
def fetch_user_data():
    """
    Example of a protected route using the @token_required decorator.
    """
    try:
        user_email = request.user_id  # not necessarily the email, depending on your token payload
        return {"message": f"Fetched user data for user_id = {user_email} successfully"}, 200
    except Exception as e:
        logging.error(f"Error in /fetch-user-data: {e}")
        return {"error": str(e)}, 500


# ------------------------------------------
# QuickBooks Login for app-based or ChatGPT sessions
# ------------------------------------------
@app.route('/quickbooks-login', methods=['GET'])
def quickbooks_login():
    """
    Initiates QuickBooks OAuth, ensuring linkage between user and tokens.
    ✅ Only updates existing states—NO new rows.
    ✅ Applies changes to all active chat sessions for the user.
    """
    try:
        # 1) Extract and decode session token
        session_token = request.cookies.get('session_token')
        if not session_token:
            return jsonify({"error": "User not authenticated. Please log in first."}), 401

        try:
            decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Session token expired. Please log in again."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid session token. Please log in again."}), 401

        user_id = decoded.get("user_id")
        if not user_id:
            return jsonify({"error": "User ID missing from session token."}), 401

        # 2) Generate a fresh OAuth state & expiry
        state = generate_random_state()
        expiry = datetime.utcnow() + timedelta(minutes=30)

        # ✅ Only UPDATE existing chat sessions for this user, don't insert new ones
        response = supabase.table("chatgpt_oauth_states").update({
            "state": state,
            "expiry": expiry.isoformat(),
            "is_authenticated": False  # Reset authentication until OAuth completes
        }).eq("user_id", user_id).execute()

        # ✅ FIX: Handle response correctly
        if not response.data:  # If no existing session, insert a new one
            logging.warning(f"No existing OAuth state found for user {user_id}. Inserting a new record...")
    
            insert_response = supabase.table("chatgpt_oauth_states").insert({
                "user_id": user_id,
                "state": state,
                "expiry": expiry.isoformat(),
                "is_authenticated": False
            }).execute()

            if not insert_response.data:
                logging.error(f"Failed to insert new OAuth state for user {user_id}")
                return jsonify({"error": "Failed to initialize OAuth state."}), 500

        logging.info(f"Updated OAuth state {state} for user {user_id}")

        # 3) Construct the QuickBooks OAuth URL with the stored 'state'
        auth_url = (
            f"{AUTHORIZATION_BASE_URL}?"
            f"client_id={CLIENT_ID}&"
            f"response_type=code&"
            f"scope={SCOPE}&"
            f"redirect_uri={REDIRECT_URI}&"
            f"state={state}"
        )
        logging.info(f"Redirecting to QuickBooks login: {auth_url}")

        # 4) Redirect the user to QuickBooks authorization page
        return redirect(auth_url)

    except Exception as e:
        logging.error(f"Error in /quickbooks-login: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500





# ------------------------------------------
# Logout Route
# ------------------------------------------
@app.route('/logout')
def logout():
    """
    Logs the user out by revoking QuickBooks tokens and deleting relevant tokens from Supabase.
    """
    try:
        session_token = request.cookies.get('session_token') or request.cookies.get('session')
        if not session_token:
            logging.warning("No session token found during logout.")
            return render_template("logout.html", message="You have been logged out successfully.")

        decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
        user_id = decoded.get("user_id")

        if not user_id:
            logging.warning("No user_id found in session token during logout.")
            return render_template("logout.html", message="You have been logged out successfully.")

        # Revoke QuickBooks tokens
        qb_response = supabase.table("quickbooks_tokens").select("refresh_token").eq("user_id", user_id).execute()
        if qb_response.data:
            refresh_token = qb_response.data[0]["refresh_token"]
            revoke_quickbooks_tokens(refresh_token)

        # Delete QuickBooks tokens
        supabase.table("quickbooks_tokens").delete().eq("user_id", user_id).execute()

        # 🔥 Expire both session tokens
        resp = make_response(render_template("logout.html", message="You have been logged out successfully."))
        resp.set_cookie("session_token", "", expires=0, path="/")
        resp.set_cookie("session", "", expires=0, path="/")

        logging.info("✅ Both session tokens deleted successfully.")
        return resp

    except Exception as e:
        logging.error(f"❌ Error during logout: {e}")
        return render_template("logout.html", message="An error occurred during logout. Please try again."), 500


# ------------------------------------------
# ChatGPT-specific routes
# ------------------------------------------

from urllib.parse import quote

@app.route('/oauth/start-for-chatgpt', methods=['GET'])
def start_oauth_for_chatgpt():
    """
    Ensures ChatGPT users have a linked user account and returns an OAuth login URL.
    If no chatSessionId is provided, generates one.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        if not chat_session_id:
            chat_session_id = str(uuid.uuid4())  # Generate a unique session ID for ChatGPT tracking
            logging.info(f"Generated new chatSessionId: {chat_session_id}")

        logging.info(f"Using chatSessionId: {chat_session_id}")

        # Check if a user is linked to this session
        user_check = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()

        if not user_check.data:
            # 🛑 No user linked to this chatSessionId → Prompt login first
            encoded_session_id = quote(chat_session_id, safe="")
            middleware_login_url = f"https://linkbooksai.com/login?chatSessionId={encoded_session_id}"
            return jsonify({
                "loginUrl": middleware_login_url,
                "chatSessionId": chat_session_id
            }), 200
        
        # ✅ A user exists, extract their user_id
        user_id = user_check.data[0]["id"]

        # ✅ Check if the user is already authenticated with QuickBooks
        auth_check = supabase.table("chatgpt_oauth_states") \
            .select("is_authenticated") \
            .eq("user_id", user_id) \
            .eq("is_authenticated", True) \
            .execute()

        is_already_authenticated = bool(auth_check.data)  # True if any previous session was authenticated

        # ✅ Store new chat session, BUT DO NOT TOUCH is_authenticated
        state = generate_random_state()
        expiry = (datetime.utcnow() + timedelta(minutes=30)).isoformat()
        supabase.table("chatgpt_oauth_states").upsert({
            "chat_session_id": chat_session_id,
            "user_id": user_id,
            "state": state,
            "expiry": expiry
        }, on_conflict=["chat_session_id"]).execute()

        # ✅ If already authenticated with QuickBooks, return success immediately
        if is_already_authenticated:
            return jsonify({
                "authenticated": True,
                "chatSessionId": chat_session_id
            }), 200

        # ✅ Otherwise, generate OAuth login link
        quickbooks_oauth_url = (
            f"{AUTHORIZATION_BASE_URL}?"
            f"client_id={CLIENT_ID}&"
            f"response_type=code&"
            f"scope={SCOPE}&"
            f"redirect_uri={REDIRECT_URI}&"
            f"state={state}"
        )

        return jsonify({
            "loginUrl": quickbooks_oauth_url,
            "chatSessionId": chat_session_id
        }), 200

    except Exception as e:
        logging.error(f"Error in start_oauth_for_chatgpt: {e}", exc_info=True)
        return jsonify({"error": "An error occurred. Please try again."}), 500




@app.route('/link-chat-session', methods=['GET'])
def link_chat_session():
    """
    Links a ChatGPT chatSessionId to the currently logged-in user via session_token.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        session_token = request.cookies.get('session_token')

        if not chat_session_id or not isinstance(chat_session_id, str) or not chat_session_id.strip():
            logging.error(f"Invalid or missing chat_session_id.")
            return jsonify({"error": "chatSessionId is required and must be a valid string."}), 400

        if not session_token:
            return jsonify({"error": "User not authenticated. Please log in first."}), 401

        try:
            decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return jsonify({"error": "Session token has expired. Please log in again."}), 401
        except jwt.InvalidTokenError:
            return jsonify({"error": "Invalid session token. Please log in again."}), 401

        user_id = decoded.get("user_id")
        if not user_id:
            return jsonify({"error": "Invalid session token: user_id not found."}), 401

        state = "initiated"
        is_authenticated = False
        expiry = (datetime.utcnow() + timedelta(minutes=30)).isoformat()

        # Check if an entry already exists
        existing_entry = supabase.table("chatgpt_oauth_states") \
            .select("chat_session_id") \
            .eq("chat_session_id", chat_session_id) \
            .eq("user_id", user_id) \
            .execute()

        if existing_entry.data:
            # Update the existing entry
            logging.info(f"Updating existing chatgpt_oauth_states entry for chatSessionId {chat_session_id}")
            oauth_states_response = supabase.table("chatgpt_oauth_states") \
                .update({
                    "state": state,
                    "expiry": expiry,
                    "is_authenticated": is_authenticated
                }) \
                .eq("chat_session_id", chat_session_id) \
                .eq("user_id", user_id) \
                .execute()
        else:
            # Insert new entry
            logging.info(f"Inserting new chatgpt_oauth_states entry for chatSessionId {chat_session_id}")
            oauth_states_response = supabase.table("chatgpt_oauth_states") \
                .insert({
                    "chat_session_id": chat_session_id,
                    "user_id": user_id,
                    "state": state,
                    "expiry": expiry,
                    "is_authenticated": is_authenticated,
                }) \
                .execute()

        if not oauth_states_response.data:
            logging.error(f"Failed to update/insert chatgpt_oauth_states for user {user_id}: {oauth_states_response}")
            return jsonify({"error": "Failed to link chatSessionId to user"}), 500

        logging.info(f"Successfully linked chatSessionId {chat_session_id} for user {user_id}.")

        # Update user profile with chatSessionId
        profile_update_payload = {
            "chat_session_id": chat_session_id,
            "updated_at": datetime.utcnow().isoformat(),
        }
        logging.info(f"Updating user_profiles: {profile_update_payload}")

        profile_update_response = supabase.table("user_profiles") \
            .update(profile_update_payload) \
            .eq("id", user_id) \
            .execute()

        if not profile_update_response.data:
            logging.error(f"Failed to update user_profiles for user {user_id}: {profile_update_response}")
            return jsonify({"error": "Failed to update user profile with chatSessionId"}), 500

        logging.info(f"chatSessionId {chat_session_id} successfully linked for user {user_id}.")

        # ✅ Keep the redirect to the dashboard with chatSessionId in the URL
        dashboard_url = url_for('dashboard', chatSessionId=chat_session_id)
        logging.info(f"Redirecting to dashboard: {dashboard_url}")
        return redirect(dashboard_url)

    except Exception as e:
        logging.error(f"Error in /link-chat-session: {e}", exc_info=True)
        return jsonify({"error": "An unexpected error occurred. Please try again later."}), 500




@app.route('/session/status', methods=['GET'])
def get_session_status():
    """
    Checks if a ChatGPT session is active by looking for tokens in chatgpt_tokens.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        if not chat_session_id:
            return jsonify({"authenticated": False, "message": "chatSessionId is required"}), 400

        response = supabase.table("chatgpt_tokens").select("*").eq("chat_session_id", chat_session_id).execute()
        if not response.data:
            logging.warning(f"No tokens found for chatSessionId {chat_session_id}.")
            return jsonify({"authenticated": False, "message": "No tokens found. Please log in."}), 401

        tokens = response.data[0]
        expiry = datetime.fromisoformat(tokens['expiry'])
        if datetime.utcnow() > expiry:
            logging.info(f"Access token for chatSessionId {chat_session_id} expired.")
            return jsonify({"authenticated": False, "message": "Session expired. Please reauthenticate."}), 401

        logging.info(f"Session {chat_session_id} is active and authenticated.")
        return jsonify({"authenticated": True, "message": "Session is active."}), 200

    except Exception as e:
        logging.error(f"Error in /session/status: {e}")
        return jsonify({"authenticated": False, "message": "An unexpected error occurred. Try again later."}), 500


def refresh_access_token_for_chatgpt(chat_session_id, refresh_token):
    """
    Refreshes the QuickBooks access token for a ChatGPT session using Supabase.
    """
    auth_header = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
    payload = {'grant_type': 'refresh_token', 'refresh_token': refresh_token}
    headers = {'Accept': 'application/json'}

    response = requests.post(TOKEN_URL, auth=auth_header, data=payload, headers=headers)
    if response.status_code == 200:
        tokens = response.json()
        access_token = tokens['access_token']
        new_refresh_token = tokens.get('refresh_token', refresh_token)
        expiry = (datetime.utcnow() + timedelta(seconds=tokens['expires_in'])).isoformat()

        try:
            update_resp = supabase.table("chatgpt_tokens").update({
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "expiry": expiry
            }).eq("chat_session_id", chat_session_id).execute()

            if not update_resp.data: # Ensure update was successful
                raise Exception("Failed to update tokens in Supabase")

            logging.info(f"Access token refreshed for ChatGPT session {chat_session_id}")
            return {
                "access_token": access_token,
                "refresh_token": new_refresh_token,
                "expiry": expiry
            }
        except Exception as e:
            logging.error(f"Failed to store refreshed tokens for ChatGPT session {chat_session_id}: {e}")
            raise
    else:
        logging.error(f"Failed to refresh access token for chatSessionId {chat_session_id}: {response.text}")
        raise Exception(response.text)

# ------------------------------------------
# Preferences
# ------------------------------------------
@app.route('/preferences', methods=['GET'])
def fetch_preferences():
    """
    Fetches the personalization note from user_profiles for a given ChatGPT session ID.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        if not chat_session_id:
            return jsonify({"error": "chatSessionId is required"}), 400

        # If your DB schema stores this in 'chat_session_id':
        response = supabase.table("user_profiles").select("personalization_note").eq("chat_session_id", chat_session_id).execute()
        if not response.data or not response.data[0].get('personalization_note'):
            return jsonify({
                "personalizationNote": "",
                "message": "No personalization note found. Please add one."
            }), 200

        personalization_note = response.data[0]['personalization_note']
        return jsonify({
            "personalizationNote": personalization_note,
            "message": "Personalization preferences retrieved successfully."
        }), 200
    except Exception as e:
        logging.error(f"Error in /preferences: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/preferences/update', methods=['POST'])
def update_preferences():
    """
    Updates the personalization note for a given ChatGPT session ID.
    """
    try:
        data = request.json
        chat_session_id = data.get('chatSessionId')
        personalization_note = data.get('personalizationNote')

        if not chat_session_id:
            return jsonify({"error": "chatSessionId is required"}), 400
        if not personalization_note:
            return jsonify({"error": "personalizationNote is required"}), 400
        if len(personalization_note) > 240:
            return jsonify({"error": "personalizationNote exceeds 240 characters"}), 400

        response = supabase.table("user_profiles").update({
            "personalization_note": personalization_note
        }).eq("chat_session_id", chat_session_id).execute()

        if not response.data:
            return jsonify({"error": "Failed to update personalization note. Invalid chatSessionId?"}), 400

        return jsonify({"message": "Personalization note updated successfully."}), 200

    except Exception as e:
        logging.error(f"Error in /preferences/update: {e}")
        return jsonify({"error": str(e)}), 500

# ------------------------------------------
# QuickBooks OAuth Callback
# ------------------------------------------
@app.route('/callback', methods=['GET'])
def callback():
    """
    Handles QuickBooks OAuth callback and stores tokens in Supabase.
    Now stores tokens **by user_id**, not chatSessionId.
    """
    try:
        # Cleanup expired states first
        cleanup_expired_states()

        code = request.args.get('code')
        realm_id = request.args.get('realmId')
        state = request.args.get('state')

        if not code or not realm_id or not state:
            logging.error("Missing required parameters (code, realmId, or state).")
            return jsonify({"error": "Missing required parameters (code, realmId, or state)."}), 400

        # 1) Look up the user_id associated with this state
        logging.info(f"Validating state: {state}")
        response_state = supabase.table("chatgpt_oauth_states").select("*").eq("state", state).execute()

        if not response_state.data:
            logging.error(f"Invalid or expired state parameter: {state}")
            return jsonify({"error": "Invalid or expired state parameter."}), 400

        stored_state = response_state.data[0]
        user_id = stored_state.get("user_id")

        if not user_id:
            logging.error("No user_id found for this OAuth state.")
            return jsonify({"error": "User ID missing from OAuth flow."}), 400

        # 2) Exchange authorization code for QuickBooks tokens
        auth_header = requests.auth.HTTPBasicAuth(CLIENT_ID, CLIENT_SECRET)
        payload = {
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': REDIRECT_URI
        }
        headers = {'Accept': 'application/json'}
        token_response = requests.post(TOKEN_URL, auth=auth_header, data=payload, headers=headers)

        if token_response.status_code != 200:
            logging.error(f"Token exchange failed: {token_response.text}")
            return jsonify({"error": f"Failed to retrieve tokens: {token_response.text}"}), 400

        tokens = token_response.json()
        access_token = tokens['access_token']
        refresh_token = tokens.get('refresh_token')
        expires_in = tokens['expires_in']
        expiry_str = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        # 3) Store tokens **tied to user_id**, NOT chatSessionId
        try:
            supabase.table("quickbooks_tokens").upsert({
                "user_id": user_id,  # Store tokens per user
                "realm_id": realm_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_expiry": expiry_str
            }).execute()

            # ✅ Mark ALL chat sessions for this user as authenticated
            supabase.table("chatgpt_oauth_states").update({
                "is_authenticated": True
            }).eq("user_id", user_id).execute()

            logging.info(f"QuickBooks authorization successful for user {user_id}")

            # ✅ Detect if running in local dev mode
            if os.getenv("FLASK_ENV") == "development":
                redirect_url = "http://localhost:5173/dashboard?quickbooks_login_success=true"
            else:
                redirect_url = "https://linkbooksai.com/dashboard?quickbooks_login_success=true"

            return redirect(redirect_url)

        except Exception as e:
            logging.error(f"Failed to store QuickBooks tokens for user {user_id}: {e}")
            return jsonify({"error": "Failed to store QuickBooks tokens."}), 500

    except Exception as e:
        logging.error(f"Error in /callback: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



# ------------------------------------------------------------------------------
# User Profile and Settings Routes
# ------------------------------------------------------------------------------
# ------------------------------------------
# User Profile API Route (Now Returns JSON)
# ------------------------------------------
@app.route('/user_profile', methods=['GET'])
@token_required  # ✅ Ensures authentication
def user_profile():
    try:
        user_id = request.user_id  # ✅ Get user_id from token_required decorator
        
        # ✅ Fetch user data from Supabase
        user_data = supabase.table("user_profiles").select("*").eq("id", user_id).execute()

        if not user_data.data:
            return jsonify({"error": "User not found"}), 404

        return jsonify(user_data.data[0])  # ✅ Send JSON response instead of rendering HTML

    except Exception as e:
        logging.error(f"Error in user_profile: {e}")
        return jsonify({"error": "Failed to load user profile"}), 500


@app.route('/settings')
@token_required  # Add authentication check
def settings():
    try:
        settings_type = request.args.get('type', 'general')  # Get settings type from URL
        return render_template('settings.html', settings_type=settings_type)
    except Exception as e:
        logging.error(f"Error in settings: {e}")
        return render_template('error.html', error="Failed to load settings"), 500

# ------------------------------------------
# Dashboard
# ------------------------------------------
@app.route('/dashboard', methods=['GET'])
def dashboard():
    """
    Dashboard now serves the Svelte frontend.
    Actual data (QuickBooks status & ChatGPT sessions) is fetched via API.
    """
    token = request.cookies.get('session_token')
    if not token:
        return redirect('/login')

    return send_from_directory(app.static_folder, "index.html")

@app.route('/api/dashboard-data', methods=['GET'])
def get_dashboard_data():
    """
    API endpoint for fetching dashboard-related data.
    Includes QuickBooks connection status & ChatGPT sessions.
    """
    try:
        # Get session token
        token = request.cookies.get('session_token')
        if not token:
            return jsonify({"error": "User not authenticated"}), 401

        try:
            decoded = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            user_id = decoded.get("user_id")
        except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
            return jsonify({"error": "Invalid or expired token"}), 401

        # ✅ QuickBooks Status Check
        quickbooks_login_needed = True
        response = supabase.table("quickbooks_tokens").select("access_token", "token_expiry").eq("user_id", user_id).execute()
        if response.data and response.data[0].get("access_token"):
            expiry = response.data[0].get("token_expiry")
            if expiry and datetime.utcnow() < datetime.fromisoformat(expiry):
                quickbooks_login_needed = False  # ✅ QuickBooks is connected

        # ✅ Fetch active ChatGPT sessions
        chatgpt_sessions = []
        session_response = supabase.table("chatgpt_oauth_states") \
            .select("chat_session_id, expiry, created_at") \
            .eq("user_id", user_id) \
            .execute()
        
        if session_response.data:
            chatgpt_sessions = [
                {
                    "chatSessionId": str(session["chat_session_id"]) if isinstance(session["chat_session_id"], uuid.UUID) else session["chat_session_id"],
                    "expiry": session["expiry"],
                    "createdAt": session.get("created_at")
                }
                for session in session_response.data
                if session.get("chat_session_id")
            ]

        # ✅ Return data as JSON
        return jsonify({
            "success": True,
            "quickbooks_login_needed": quickbooks_login_needed,
            "chatSessionId": request.args.get('chatSessionId', ""),  # Pass chatSessionId from URL
            "chatgpt_sessions": chatgpt_sessions
        }), 200

    except Exception as e:
        logging.error(f"Error in /api/dashboard-data: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500



# ------------------------------------------
# Fetch Reports for ChatGPT sessions
# ------------------------------------------
@app.route('/fetch-reports', methods=['GET'])
def fetch_reports_route():
    """
    Fetches a QuickBooks report for a given chatSessionId or userId,
    and delegates token handling + refresh logic to fetch_report().
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        session_token = request.cookies.get('session_token')

        # 1) If we have a session token, decode user_id
        user_id = None
        if session_token:
            try:
                decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
                user_id = decoded.get("user_id")
            except jwt.ExpiredSignatureError:
                logging.error("Session token expired.")
                return jsonify({"error": "Session token expired. Please log in again."}), 401
            except jwt.InvalidTokenError:
                logging.error("Invalid session token.")
                return jsonify({"error": "Invalid session token. Please log in again."}), 401

        # 2) If no user_id from session_token, but we have chatSessionId, find user_id from user_profiles
        if not user_id and chat_session_id:
            user_lookup = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()
            if user_lookup.data:
                user_id = user_lookup.data[0]["id"]
            else:
                logging.error(f"No user found for chatSessionId: {chat_session_id}")
                return jsonify({"error": "User not found for given chatSessionId"}), 404

        if not user_id:
            logging.error("No user_id found via session token or chat_session_id.")
            return jsonify({"error": "No user_id found. Please log in or link session."}), 401

        # 3) Extract the needed query parameters
        report_type = request.args.get("reportType")
        start_date = request.args.get("startDate")
        end_date = request.args.get("endDate")

        # 4) Let fetch_report handle retrieval/refresh of tokens
        report_data = fetch_report(
            user_id=user_id,
            report_type=report_type,
            start_date=start_date,
            end_date=end_date
        )

        # 5) Return the resulting JSON
        return jsonify({
            "reportType": report_type,
            "data": report_data
        }), 200

    except Exception as e:
        logging.error(f"Error in /fetch-reports: {e}")
        return jsonify({"error": str(e)}), 500
    
    
#-------------Fetch Transactions Helpers-------------#

def get_qb_transactions_raw(user_id: str, start_date: str, end_date: str) -> list:
    """
    Calls the QuickBooks TransactionList report for the given user_id and date range.
    Returns a list of transaction dicts (with keys like date, transaction_type, name, etc.).
    No local filtering is applied here.

    :param user_id: The user whose tokens to use.
    :param start_date: e.g. '2024-08-01' (YYYY-MM-DD)
    :param end_date: e.g. '2024-08-31' (YYYY-MM-DD)
    :return: List of dicts, each representing a single transaction row.
    """
    # 1) Retrieve and refresh tokens if necessary
    tokens = get_quickbooks_tokens(user_id)
    access_token = tokens.get("access_token")
    realm_id = tokens.get("realm_id")
    expiry_str = tokens.get("token_expiry")

    if not access_token or not realm_id:
        raise Exception("Missing QuickBooks tokens or realm_id for this user.")

    if expiry_str:
        expiry_dt = datetime.fromisoformat(expiry_str)
        if datetime.utcnow() > expiry_dt:
            logging.info("Token expired; refreshing tokens...")
            refresh_access_token(user_id)
            tokens = get_quickbooks_tokens(user_id)
            access_token = tokens.get("access_token")
            realm_id = tokens.get("realm_id")

    # 2) Call the QuickBooks TransactionList endpoint
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json"
    }
    base_url = f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/reports/TransactionList"
    params = {
        "start_date": start_date,
        "end_date": end_date
    }
    resp = requests.get(base_url, headers=headers, params=params)

    # Check for mid-request expiry
    if resp.status_code == 401:
        logging.info("Token might have expired again. Attempting a second refresh...")
        refresh_access_token(user_id)
        tokens = get_quickbooks_tokens(user_id)
        access_token = tokens.get("access_token")
        headers["Authorization"] = f"Bearer {access_token}"
        resp = requests.get(base_url, headers=headers, params=params)

    if resp.status_code != 200:
        logging.error(f"Error fetching TransactionList: {resp.text}")
        raise Exception(f"QuickBooks API error: {resp.status_code} {resp.text}")

    # 3) Parse the JSON
    data = resp.json()
    rows = data.get("Rows", {}).get("Row", [])
    transactions = []

    # We know from your JSON that:
    #  Col 0 => Date
    #  Col 1 => Transaction Type
    #  Col 2 => Doc Num (No.)
    #  Col 3 => Posting (Yes/No)
    #  Col 4 => Name (Vendor, Customer, etc.)
    #  Col 5 => Memo/Description
    #  Col 6 => Account
    #  Col 7 => Split
    #  Col 8 => Amount

    for row in rows:
        # If the row has nested rows:
        if "Rows" in row and "Row" in row["Rows"]:
            for data_row in row["Rows"]["Row"]:
                if data_row.get("type") != "Data":
                    continue
                col_data = data_row.get("ColData", [])
                if len(col_data) < 9:
                    continue
                transactions.append({
                    "date": col_data[0].get("value", ""),
                    "transaction_type": col_data[1].get("value", ""),
                    "doc_num": col_data[2].get("value", ""),
                    "posting": col_data[3].get("value", ""),
                    "name": col_data[4].get("value", ""),
                    "memo": col_data[5].get("value", ""),
                    "account": col_data[6].get("value", ""),
                    "split": col_data[7].get("value", ""),
                    "amount": col_data[8].get("value", "")
                })
        else:
            # Standalone row
            if row.get("type") != "Data":
                continue
            col_data = row.get("ColData", [])
            if len(col_data) < 9:
                continue
            transactions.append({
                "date": col_data[0].get("value", ""),
                "transaction_type": col_data[1].get("value", ""),
                "doc_num": col_data[2].get("value", ""),
                "posting": col_data[3].get("value", ""),
                "name": col_data[4].get("value", ""),
                "memo": col_data[5].get("value", ""),
                "account": col_data[6].get("value", ""),
                "split": col_data[7].get("value", ""),
                "amount": col_data[8].get("value", "")
            })

    return transactions


def should_use_gpt4(query):
    """
    Determines if GPT-4 Turbo should be used based on complexity.
    """
    if len(query) > 100:  # Example: Longer queries are likely more complex
        return True
    keywords = ["approximate", "similar to", "fuzzy match", "group by", "trend"]
    if any(kw in query.lower() for kw in keywords):  # Keywords suggest deeper reasoning
        return True
    return False

import openai

def ask_gpt_to_filter(transactions, query, model):
    """
    Sends transactions and a query to OpenAI for intelligent filtering.
    """
    openai_client = openai.OpenAI()  # Ensure OpenAI client is initialized properly

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are an AI that analyzes financial transactions and filters them based on user requests."},
            {"role": "user", "content": f"Here are my transactions:\n{transactions}\n\nFilter them based on this request: {query}"}
        ]
    )
    return response.choices[0].message.content  # Corrected path to response content

#-------------- Fetch Transactions AI Route --------------#

@app.route('/fetch-transactions-ai', methods=['GET'])
def fetch_transactions_ai():
    """
    Fetches transactions from QuickBooks, then processes them using OpenAI for intelligent filtering.
    
    Example:
    https://linkbooksai.com/fetch-transactions-ai?start_date=2024-08-01&end_date=2024-08-31&query=Find all food places over £20
    
    The 'query' parameter is used to instruct OpenAI on filtering.
    """
    try:
        # 1️⃣ Get session token from cookies
        session_token = request.cookies.get('session_token')
        if not session_token:
            return jsonify({"error": "No session token provided."}), 401

        # 2️⃣ Decode session token to get user_id
        try:
            decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
            user_id = decoded.get("user_id")
        except Exception as e:
            logging.error("Error decoding session token: " + str(e))
            return jsonify({"error": "Invalid or expired session token."}), 401

        if not user_id:
            return jsonify({"error": "No user_id found in session token."}), 401

        # 3️⃣ Get date range & AI query from query params
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        query = request.args.get("query")  # Example: "Find all food places over £20"

        if not start_date or not end_date or not query:
            return jsonify({"error": "start_date, end_date, and query parameters are required."}), 400

        # 4️⃣ Fetch all transactions from QuickBooks
        transactions = get_qb_transactions_raw(user_id, start_date, end_date)

        # 5️⃣ Determine AI Model (GPT-3.5 Turbo vs GPT-4 Turbo)
        use_gpt4 = should_use_gpt4(query)
        model = "gpt-4-turbo" if use_gpt4 else "gpt-3.5-turbo"

        logging.info(f"Using {model} for AI filtering")

        # 6️⃣ Call OpenAI for filtering
        gpt_response = ask_gpt_to_filter(transactions, query, model)

        # 7️⃣ Return the AI-filtered transactions
        return jsonify({"transactions": gpt_response}), 200

    except Exception as e:
        logging.error("Error in /fetch-transactions-ai: " + str(e))
        return jsonify({"error": str(e)}), 500


#-------------- Fetch Transactions Classic Route --------------#
@app.route('/fetch-transactions', methods=['GET'])
def fetch_transactions_route():
    """
    Fetches the TransactionList report from QuickBooks using dynamic query parameters.
    
    Query parameters such as startDate, endDate, date_macro, payment_method, etc. are sent directly.
    Parameters like vendor, customer, name, department, and memo are removed from the QB request
    and applied locally after the full report is returned.
    
    Example usage:
      /fetch-transactions?chatSessionId=...&startDate=2024-08-01&endDate=2024-08-31&vendor=McDonald's
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        session_token = request.cookies.get('session_token')
        user_id = None

        # 1) Extract user_id from the session token if available.
        if session_token:
            try:
                decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
                user_id = decoded.get("user_id")
            except jwt.ExpiredSignatureError:
                logging.error("Session token expired.")
                return jsonify({"error": "Session token expired. Please log in again."}), 401
            except jwt.InvalidTokenError:
                logging.error("Invalid session token.")
                return jsonify({"error": "Invalid session token. Please log in again."}), 401

        # 2) If not available, look up user_id via chatSessionId.
        if not user_id and chat_session_id:
            user_lookup = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()
            if user_lookup.data:
                user_id = user_lookup.data[0]["id"]
            else:
                logging.error(f"No user found for chatSessionId: {chat_session_id}")
                return jsonify({"error": "User not found for given chatSessionId"}), 404

        if not user_id:
            logging.error("No user_id found via session token or chatSessionId.")
            return jsonify({"error": "No user_id found. Please log in or link session."}), 401

        # 3) Build a dictionary of allowed QuickBooks query parameters from the request.
        allowed_params = [
            "date_macro", "payment_method", "duedate_macro", "arpaid", "bothamount",
            "transaction_type", "docnum", "start_moddate", "source_account_type",
            "group_by", "start_date", "department", "start_duedate", "columns",
            "end_duedate", "end_date", "memo", "appaid", "moddate_macro", "printed",
            "createdate_macro", "cleared", "customer", "qzurl", "term", "end_createdate",
            "name", "sort_by", "sort_order", "start_createdate", "end_moddate"
        ]
        qb_params = {}
        for param in allowed_params:
            value = request.args.get(param)
            if value is not None:
                qb_params[param] = value

        # 4) Also capture parameters that require local filtering (e.g., vendor).
        for key in ["vendor"]:
            value = request.args.get(key)
            if value is not None:
                qb_params[key] = value

        # 5) Call the helper to fetch and filter transactions.
        transactions_data = fetch_transactions(user_id=user_id, qb_params=qb_params)

        # 6) Return the sanitized transactions.
        return jsonify({"data": transactions_data}), 200

    except Exception as e:
        logging.error(f"Error in /fetch-transactions: {e}")
        return jsonify({"error": str(e)}), 500
    
    
@app.route('/filter-transactions', methods=['POST'])
def filter_transactions():
    try:
        data = request.json
        query = data.get("query")
        user_id = data.get("user_id")
        start_date = data.get("start_date")
        end_date = data.get("end_date")
        transactions = get_qb_transactions_raw(user_id, start_date, end_date)

        # Determine complexity
        use_gpt4 = should_use_gpt4(query)

        # Select model
        model = "gpt-4-turbo" if use_gpt4 else "gpt-3.5-turbo"

        logging.info(f"Using {model} for filtering")

        gpt_response = ask_gpt_to_filter(transactions, query, model)
        return jsonify(gpt_response)

    except Exception as e:
        logging.error(f"Error in /filter-transactions: {str(e)}")
        return jsonify({"error": str(e)}), 500


def filter_transactions_local(transactions: list, tx_type_filter: str = None, name_filter: str = None) -> list:
    """
    Filters the list of transaction dicts based on:
      - transaction_type (exact or partial match),
      - name (exact or partial match).
    Matching is done case-insensitively.
    
    :param transactions: The raw list of transaction dicts (date, transaction_type, name, etc.)
    :param tx_type_filter: A string to filter transaction_type (e.g. 'Expense', 'Invoice', etc.).
    :param name_filter: A string to filter the name column (e.g. 'Amazon', 'Warner Bros').
    :return: A new list of transactions that match both filters (if provided).
    """
    filtered = []
    for txn in transactions:
        # By default, include this transaction unless it fails a filter.
        include = True

        if tx_type_filter:
            # If the user typed e.g. 'expense', we can do a case-insensitive substring check:
            if tx_type_filter.lower() not in txn["transaction_type"].lower():
                include = False

        if name_filter and include:  # Only check name if still included
            if name_filter.lower() not in txn["name"].lower():
                include = False

        if include:
            filtered.append(txn)
    return filtered


# ------------------------------------------
# Business Info
# ------------------------------------------
@app.route('/business-info', methods=['GET'])
def business_info():
    """
    Retrieves the user's company information from QuickBooks if they have a valid session.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        session_token = request.cookies.get('session_token')

        if not chat_session_id and not session_token:
            logging.error("Missing chatSessionId and session token.")
            return jsonify({"error": "chatSessionId or session token is required"}), 400

        user_id = None
        if session_token:
            try:
                decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
                user_id = decoded.get("user_id")
            except jwt.ExpiredSignatureError:
                logging.error("Session token expired.")
                return jsonify({"error": "Session token expired. Please log in again."}), 401
            except jwt.InvalidTokenError:
                logging.error("Invalid session token.")
                return jsonify({"error": "Invalid session token. Please log in again."}), 401

        # -- If chatSessionId is provided, find the user_id first --
        if chat_session_id and not user_id:
            user_lookup = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()
            if user_lookup.data:
                user_id = user_lookup.data[0]["id"]
            else:
                logging.error(f"No user found for chatSessionId: {chat_session_id}")
                return jsonify({"error": "User not found for given chatSessionId"}), 404

        if not user_id:
            logging.error("No valid identifier for token retrieval.")
            return jsonify({"error": "No valid identifier for token retrieval."}), 400

        # -- Fetch QuickBooks tokens using user_id --
        tokens_response = supabase.table("quickbooks_tokens").select("*").eq("user_id", user_id).execute()

        if not tokens_response.data:
            logging.error(f"No tokens found for user {user_id}")
            return jsonify({"error": "No QuickBooks tokens found. Please log in again."}), 404

        tokens = tokens_response.data[0]
        access_token = tokens["access_token"]
        realm_id = tokens["realm_id"]
        expiry = tokens["token_expiry"]

        if not access_token or not realm_id:
            logging.error("Missing access_token or realm_id.")
            return jsonify({"error": "Invalid QuickBooks tokens."}), 400

        # -- Check for expiry / refresh if needed --
        if expiry and datetime.utcnow() > datetime.fromisoformat(expiry):
            logging.info("Access token expired. Attempting refresh...")
            try:
                refresh_access_token(user_id)  # Refresh the token
                updated_tokens = supabase.table("quickbooks_tokens").select("*").eq("user_id", user_id).execute()
                if updated_tokens.data:
                    access_token = updated_tokens.data[0]["access_token"]
                    realm_id = updated_tokens.data[0]["realm_id"]
                else:
                    raise Exception("No updated tokens after refresh.")
            except Exception as e:
                logging.error(f"Failed to refresh tokens: {e}")
                return jsonify({"error": "Failed to refresh tokens. Please log in again."}), 401

        # -- Call QuickBooks API to get company info --
        company_info = get_company_info(user_id)

        return jsonify({
            "companyName": company_info.get("CompanyName"),
            "legalName": company_info.get("LegalName"),
            "address": company_info.get("CompanyAddr", {}).get("Line1"),
            "phone": company_info.get("PrimaryPhone", {}).get("FreeFormNumber"),
            "email": company_info.get("Email", {}).get("Address"),
        }), 200

    except Exception as e:
        logging.error(f"Error in /business-info: {e}")
        return jsonify({"error": str(e)}), 500



# ------------------------------------------#
#             Company Audit
# ------------------------------------------#
@app.route('/audit', methods=['GET'])
def audit():
    """
    Fetches relevant financial reports and business info from QuickBooks,
    then analyzes the company's financial health and suggests improvements.
    """
    try:
        chat_session_id = request.args.get('chatSessionId')
        session_token = request.cookies.get('session_token')

        if not chat_session_id and not session_token:
            logging.error("Missing chatSessionId and session token.")
            return jsonify({"error": "chatSessionId or session token is required"}), 400

        user_id = None
        if session_token:
            try:
                decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
                user_id = decoded.get("user_id")
            except jwt.ExpiredSignatureError:
                logging.error("Session token expired.")
                return jsonify({"error": "Session token expired. Please log in again."}), 401
            except jwt.InvalidTokenError:
                logging.error("Invalid session token.")
                return jsonify({"error": "Invalid session token. Please log in again."}), 401

        # -- If chatSessionId is provided, find the user_id first --
        if chat_session_id and not user_id:
            user_lookup = supabase.table("user_profiles").select("id").eq("chat_session_id", chat_session_id).execute()
            if user_lookup.data:
                user_id = user_lookup.data[0]["id"]
            else:
                logging.error(f"No user found for chatSessionId: {chat_session_id}")
                return jsonify({"error": "User not found for given chatSessionId"}), 404

        if not user_id:
            logging.error("No valid identifier for token retrieval.")
            return jsonify({"error": "No valid identifier for token retrieval."}), 400

        # -- Fetch company info from QuickBooks --
        try:
            company_info = get_company_info(user_id)
        except Exception as e:
            logging.error(f"Error fetching company info: {e}")
            return jsonify({"error": "Failed to retrieve company info. Please check your QuickBooks connection."}), 500

        if not company_info:
            return jsonify({"error": "Company info is empty or unavailable."}), 404

        # -- Fetch relevant financial reports --
        # Fetch relevant financial reports
        reports_to_fetch = ["ProfitAndLoss", "BalanceSheet", "CashFlow"]
        report_data = {}
        failed_reports = []

        for report in reports_to_fetch:
            try:
                report_data[report] = fetch_report(user_id, report)
            except Exception as e:
                logging.warning(f"Could not fetch {report}: {e}")
                failed_reports.append(report)

        # ✅ Instead of failing if all reports are missing, return what we got.
        if not report_data:
            return jsonify({
                "warning": "No financial reports could be retrieved. Ensure QuickBooks is connected.",
                "reports": {},
                "failedReports": failed_reports
            }), 200  # ✅ Return a success response with warning instead of failing


        # -- Construct AI Prompt for Financial Analysis --
        prompt = (
            "Analyze the following company's financial data and business information. "
            "Provide insights into financial health, risks, opportunities, and improvement strategies.\n\n"
            f"**Company Details:**\n"
            f"Company Name: {company_info.get('CompanyName')}\n"
            f"Legal Name: {company_info.get('LegalName')}\n"
            f"Address: {company_info.get('CompanyAddr', {}).get('Line1', 'N/A')}\n"
            f"Phone: {company_info.get('PrimaryPhone', {}).get('FreeFormNumber', 'N/A')}\n"
            f"Email: {company_info.get('Email', {}).get('Address', 'N/A')}\n\n"
            "**Financial Reports:**\n"
        )

        for report_name, data in report_data.items():
            prompt += f"\n**{report_name} Report:**\n{data}\n"

        prompt += "\nBased on the above data, provide an assessment of the company's financial standing, potential risks, and recommendations for improvement."

        # -- Send prompt to OpenAI for analysis --
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7
        )

        analysis = response.choices[0].message.content

        # -- Return results --
        return render_template('audit.html', analysis=analysis, data={"company_info": company_info, "reports": report_data})

    except Exception as e:
        logging.error(f"Error in /audit: {e}")
        return jsonify({"error": str(e)}), 500


# ------------------------------------------
# get_reports: Returns a list of supported QuickBooks reports
# ------------------------------------------
def get_reports():
    """
    Returns a list of supported QuickBooks reports.
    """
    return [
        # Financial Reports
        "ProfitAndLoss",
        "ProfitAndLossDetail",
        "BalanceSheet",
        "BalanceSheetDetail",
        "CashFlow",
        "TrialBalance",
        "GeneralLedger",

        # Sales Reports
        "SalesByCustomerSummary",
        "SalesByCustomerDetail",
        "SalesByProductServiceSummary",
        "SalesByProductServiceDetail",
        "SalesByLocation",
        "EstimatesByCustomer",

        # Expense and Vendor Reports
        "ExpensesByVendorSummary",
        "ExpensesByVendorDetail",
        "AgedPayablesSummary",
        "AgedPayablesDetail",
        "UnpaidBills",

        # Customer Reports
        "AgedReceivablesSummary",
        "AgedReceivablesDetail",
        "CustomerBalanceSummary",
        "CustomerBalanceDetail",
        "InvoiceList",

        # Employee Reports
        "PayrollSummary",
        "PayrollDetails",
        "EmployeeDetails",
        "TimeActivitiesByEmployeeDetail",

        # Product and Inventory Reports
        "InventoryValuationSummary",
        "InventoryValuationDetail",
        "PhysicalInventoryWorksheet",
        "ProductServiceList",

        # Budget and Forecast Reports
        "BudgetOverview",
        "BudgetVsActual",
        "ProfitAndLossBudgetPerformance",

        # Tax and VAT Reports
        "VATSummary",
        "VATDetailReport",
        "SalesTaxLiabilityReport",

        # Custom Reports
        "CustomSummaryReport",
        "CustomTransactionDetailReport",

        # Other Reports
        "TransactionListByDate",
        "AuditLog",
        "BusinessSnapshot",
        "MissingChecks",
        "ReconciliationReports"
    ]

@app.route('/list-reports', methods=['GET'])
def list_reports():
    """
    Returns a list of supported QuickBooks reports.
    """
    try:
        available_reports = get_reports()
        return {
            "availableReports": available_reports,
            "message": "Use the /fetch-reports endpoint with ?reportType=<one of these>."
        }, 200
    except Exception as e:
        logging.error(f"Error listing reports: {e}")
        return {"error": str(e)}, 500

# ------------------------------------------
# analyze-reports
# ------------------------------------------
@app.route('/analyze-reports', methods=['POST'])
def analyze_reports():
    """
    Sends retrieved report data to OpenAI for an example analysis.
    """
    try:
        reports = request.json
        if not reports or not isinstance(reports, dict):
            return {"error": "Invalid or missing report data. Expected a JSON object."}, 400

        prompt = f"Analyze the following financial data:\n{reports}"
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        analysis = response.choices[0].message.content
        return {"analysis": analysis, "originalData": reports}, 200
    except Exception as e:
        logging.error(f"Error analyzing reports: {e}")
        return {"error": str(e)}, 500
    
# ------------------------------------------#
#            Chat Stuff                     #
# ------------------------------------------#
class StreamHandler(AssistantEventHandler):
    """
    Handles streaming AI responses via WebSockets.
    """
    def __init__(self, thread_id):
        super().__init__()
        self.response_text = ""
        self.thread_id = thread_id  # Store thread ID to associate responses

    def on_text_created(self, text):
        print("\nAssistant:", end="", flush=True)

    def on_text_delta(self, delta, snapshot):
        if delta.value:
            self.response_text += delta.value
            print(delta.value, end="", flush=True)

            # 🔹 Send each chunk via WebSockets to the frontend
            socketio.emit('chat_response', {'thread_id': self.thread_id, 'data': delta.value})

    def on_tool_call_created(self, tool_call):
        print(f"\nAssistant used tool: {tool_call.type}")

    def on_tool_call_delta(self, delta, snapshot):
        if delta.type == "code_interpreter" and delta.code_interpreter.input:
            print(delta.code_interpreter.input, end="", flush=True)

    def get_response(self):
        return self.response_text


# ------------------------------------------#
#     Process and Stream Response          #
# ------------------------------------------#
def process_and_stream_response(user_id, user_message):
    print(f"🔄 Processing message for user {user_id}: {user_message}", flush=True)

    # ✅ Retrieve thread_id from Supabase
    thread_query = supabase.table("user_threads").select("thread_id").eq("user_id", user_id).execute()
    thread_id = thread_query.data[0]["thread_id"] if thread_query.data else None

    if not thread_id:
        print("🆕 Creating new chat thread...", flush=True)
        thread = openai_client.beta.threads.create()
        thread_id = thread.id
        supabase.table("user_threads").insert({"user_id": user_id, "thread_id": thread_id}).execute()

    print(f"🟢 Using thread_id: {thread_id} for user {user_id}", flush=True)

    # ✅ Add user message to the thread
    openai_client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_message
    )

    print(f"📩 Chat message added to thread {thread_id}, streaming response...", flush=True)

    # ✅ Stream response from OpenAI
    handler = StreamHandler(thread_id)

    try:
        with openai_client.beta.threads.runs.stream(
            thread_id=thread_id,
            assistant_id=ASSISTANT_ID,
            event_handler=handler
        ) as stream:
            for chunk in stream:
                if chunk.event == "text_delta":
                    print(f"📡 Sending to WebSocket: {chunk.data.delta.value} (Thread: {thread_id})", flush=True)

                    # ✅ Ensure `thread_id` is always sent
                    socketio.emit("chat_response", {
                        "thread_id": thread_id if thread_id else "UNKNOWN_THREAD",
                        "data": chunk.data.delta.value if chunk.data.delta.value else "[NO DATA]"
                }, namespace="/")


    except Exception as e:
        print(f"❌ [ERROR] Streaming error: {str(e)}", flush=True)
        socketio.emit("chat_response", {
            "thread_id": thread_id,
            "data": "[ERROR] An error occurred."
        })
    finally:
        print(f"✅ WebSocket: Sent [DONE] (Thread: {thread_id})", flush=True)
        socketio.emit("chat_response", {
            "thread_id": thread_id,
            "data": "[DONE]"
        })


# ------------------------------------------#
#          Socket.IO Event Handlers          #
# ------------------------------------------#
@socketio.on('connect')
def on_connect():
    print("✅ Client connected", flush=True)
    emit('chat_response', {'data': "Connected to WebSocket"})

@socketio.on('disconnect')
def on_disconnect():
    print("❌ Client disconnected", flush=True)


@socketio.on('chat_message')
def handle_chat_message(data):
    """
    Handles incoming WebSocket messages and streams OpenAI responses.
    """
    sid = request.sid  # Get WebSocket session ID
    session_token = data.get("session_token")
    user_message = data.get("message")

    if not session_token:
        emit("chat_response", {"error": "No session token provided"}, room=sid)
        return

    # ✅ Decode JWT token
    try:
        decoded = jwt.decode(session_token, SECRET_KEY, algorithms=["HS256"])
        user_id = decoded.get("user_id")
    except (ExpiredSignatureError, InvalidTokenError) as e:
        emit("chat_response", {"error": "Invalid session token"}, room=sid)
        return

    # ✅ Retrieve or create thread_id
    thread_query = supabase.table("user_threads").select("thread_id").eq("user_id", user_id).execute()
    thread_id = thread_query.data[0]["thread_id"] if thread_query.data else None

    if not thread_id:
        thread = openai_client.beta.threads.create()
        thread_id = thread.id
        supabase.table("user_threads").insert({"user_id": user_id, "thread_id": thread_id}).execute()

    # ✅ Add user message to OpenAI thread
    openai_client.beta.threads.messages.create(
        thread_id=thread_id,
        role="user",
        content=user_message
    )

    # ✅ Stream response from OpenAI
    print(f"📡 Streaming response from OpenAI (Thread: {thread_id})")
    
    stream = openai_client.beta.threads.runs.create(
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID,
        stream=True
    )

    for event in stream:
        if event.event == "thread.message.delta":
            chunk = event.data.delta.content[0].text.value
            socketio.emit("chat_response", {"thread_id": thread_id, "data": chunk}, room=sid)
            eventlet.sleep(0)  # ✅ Prevent blocking

    # ✅ Signal completion
    socketio.emit("chat_response", {"thread_id": thread_id, "data": "[DONE]"}, room=sid)


# ✅ Verify Assistant Configuration
try:
    assistant = openai_client.beta.assistants.retrieve(ASSISTANT_ID)
    logging.info(f"✅ Assistant Loaded: {assistant.name} ({ASSISTANT_ID})")
except Exception as e:
    logging.error(f"❌ Error retrieving assistant: {str(e)}")
    
# ✅ Update Assistant Instructions
assistant = openai_client.beta.assistants.update(
    assistant_id=ASSISTANT_ID,
    instructions=(
        "You are an AI assistant that helps users with QuickBooks transactions and invoices. "
        "You should remember previous interactions within a thread and provide context-aware responses. "
        "If a user asks follow-up questions, ensure you refer to previous discussions in the same thread."
    )
)


# ------------------------------------------
# Testing OpenAI Endpoints
# ------------------------------------------
@app.route('/test-openai', methods=['GET'])
def test_openai():
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello, can you confirm this is working?"}
            ],
            max_tokens=50
        )
        return {"message": response.choices[0].message.content}, 200
    except Exception as e:
        logging.error(f"Error in /test-openai: {e}")
        return {"error": f"OpenAI error: {str(e)}"}, 500

@app.route('/test-openai-key', methods=['GET'])
def test_openai_key():
    try:
        if not openai_client.api_key:
            raise ValueError("OpenAI API key not loaded")
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Test API key"}],
            max_tokens=10
        )
        return {"response": response['choices'][0]['message']['content']}, 200
    except Exception as e:
        return {"error": str(e)}, 500

# ------------------------------------------
# Misc: EULA, Privacy, Debug
# ------------------------------------------
@app.route('/eula', methods=['GET'])
def eula():
    return render_template('eula.html')

@app.route('/privacy-policy', methods=['GET'])
def privacy_policy():
    return render_template('privacy_policy.html')

@app.route('/debug-env', methods=['GET'])
def debug_env():
    """
    Be careful not to expose real secrets in production logs/responses.
    """
    if not DEV_MODE:
        return jsonify({"error": "Not authorized."}), 403

    variables = {
        "SUPABASE_URL": os.getenv('SUPABASE_URL'),
        "SUPABASE_KEY": os.getenv('SUPABASE_KEY'),
        "QB_SANDBOX_CLIENT_ID": os.getenv('QB_SANDBOX_CLIENT_ID'),
        "QB_SANDBOX_CLIENT_SECRET": os.getenv('QB_SANDBOX_CLIENT_SECRET'),
        "QB_PROD_CLIENT_ID": os.getenv('QB_PROD_CLIENT_ID'),
        "QB_PROD_CLIENT_SECRET": os.getenv('QB_PROD_CLIENT_SECRET'),
        "FLASK_SECRET_KEY": os.getenv('FLASK_SECRET_KEY'),
        "OPENAI_API_KEY": os.getenv('OPENAI_API_KEY'),
    }
    logging.info(f"Environment variables: {variables}")
    return {
        key: ("*****" if "KEY" in key or "SECRET" in key else value)
        for key, value in variables.items()
    }, 200


@app.before_request
def log_request_info():
    logging.info(f"📩 Incoming request: {request.method} {request.path}")


#---------- Start the Flask Server ----------#

if os.getenv("FLASK_ENV") == "production":
    app.debug = False

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=app.debug)
