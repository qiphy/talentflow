import os
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from zai import ZaiClient

load_dotenv()

app = FastAPI()

# Clients
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
zai_client = ZaiClient(api_key=os.getenv("Z_AI_API_KEY"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],            # or list specific origins like ["http://localhost:5173"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models matching your HTML form
class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: str
    role: str  # "employer" or "employee"
    company: Optional[str] = None
    accept_terms: bool

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    remember: Optional[bool] = False

# Helper to create profile row
def create_profile_row(user_id: str, payload: SignupRequest):
    profile = {
        "id": user_id,
        "full_name": payload.full_name,
        "email": payload.email,
        "phone": payload.phone,
        "role": payload.role,
        # store company only for employers
        "company": payload.company if payload.role == "employer" else None,
    }
    # upsert/insert profile
    supabase.table("profiles").insert(profile).execute()

# Signup endpoint
@app.post("/signup")
async def signup(payload: SignupRequest):
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="Terms must be accepted.")
    if payload.role not in ("employer", "employee"):
        raise HTTPException(status_code=400, detail="Invalid role.")

    try:
        # sign up (supporting client shape differences)
        try:
            auth_response = supabase.auth.sign_up({"email": payload.email, "password": payload.password})
        except AttributeError:
            auth_response = supabase.auth.sign_up(email=payload.email, password=payload.password)

        # normalize user from possible shapes
        user = None
        if isinstance(auth_response, dict):
            user = auth_response.get("user") or (auth_response.get("data") and auth_response["data"].get("user"))
        else:
            user = getattr(auth_response, "user", None) or (getattr(auth_response, "data", None) and getattr(auth_response.data, "user", None))

        if not user:
            err = (auth_response.get("error") if isinstance(auth_response, dict) else getattr(auth_response, "error", None)) or "Failed to create user."
            raise Exception(err)

        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

        # insert profile row using upsert to avoid duplicates
        profile = {
            "id": user_id,
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "role": payload.role,
            "company": payload.company if payload.role == "employer" else None,
        }
        # use upsert so existing rows are updated if present
        supabase.table("profiles").upsert(profile).execute()

        # Return role and redirect hint (no tokens in JSON)
        redirect = "/hr" if payload.role == "employer" else "/candidate"
        return {"status": "success", "message": "User created. Please log in.", "user_id": user_id, "role": payload.role, "redirect": redirect}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Login endpoint (email + password)
@app.post("/login")
async def login(req: LoginRequest, response: Response):
    try:
        # 1) sign in (support multiple client shapes)
        try:
            auth_response = supabase.auth.sign_in({"email": req.email, "password": req.password})
        except AttributeError:
            # newer client style
            auth_response = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})

        # 2) normalize session/user/access tokens from possible shapes
        session = None
        user = None

        if isinstance(auth_response, dict):
            # some clients return {"data": { "session": {...} }} or {"session": {...}}
            session = auth_response.get("session") or auth_response.get("data") or auth_response.get("data", {}).get("session")
            if isinstance(session, dict):
                user = session.get("user")
        else:
            session = getattr(auth_response, "session", None) or getattr(auth_response, "data", None)
            if session:
                user = getattr(session, "user", None)

        # fallback: some clients return access/refresh at top-level and user inside "user"
        access_token = None
        refresh_token = None
        if session:
            access_token = session.get("access_token") if isinstance(session, dict) else getattr(session, "access_token", None)
            refresh_token = session.get("refresh_token") if isinstance(session, dict) else getattr(session, "refresh_token", None)

        if not access_token:
            access_token = getattr(auth_response, "access_token", None) or (auth_response.get("access_token") if isinstance(auth_response, dict) else None)
            refresh_token = getattr(auth_response, "refresh_token", None) or (auth_response.get("refresh_token") if isinstance(auth_response, dict) else None)

        # If sign-in failed
        if not user and not access_token:
            err = (auth_response.get("error") if isinstance(auth_response, dict) else getattr(auth_response, "error", None)) or "Invalid credentials."
            raise HTTPException(status_code=401, detail=err)

        # Determine user_id
        user_id = None
        if user:
            user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
        # if user_id still missing, attempt to read from session object fields
        if not user_id and session:
            user_id = session.get("user", {}).get("id") if isinstance(session, dict) else getattr(session.user, "id", None) if hasattr(session, "user") else None

        # 3) set httpOnly cookie for access token (if available)
        if access_token:
            max_age = 7 * 24 * 3600 if req.remember else None
            response.set_cookie(key="access_token", value=access_token, httponly=True, secure=True, samesite="lax", max_age=max_age)

            # Optionally store refresh token in httpOnly cookie too (recommended)
            if refresh_token:
                refresh_max_age = 30 * 24 * 3600  # example 30 days
                response.set_cookie(key="refresh_token", value=refresh_token, httponly=True, secure=True, samesite="lax", max_age=refresh_max_age)

        # 4) query profiles table to get role
        role = None
        if user_id:
            prof_resp = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
            if isinstance(prof_resp, dict):
                role = (prof_resp.get("data") or {}).get("role")
            else:
                # client may provide .data attribute
                prof_data = getattr(prof_resp, "data", None)
                if isinstance(prof_data, dict):
                    role = prof_data.get("role")

        # default redirect based on role
        redirect = "/hr" if role == "employer" else "/candidate"

        return {"status": "success", "user_id": user_id, "role": role, "redirect": redirect}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Logout
@app.post("/logout")
async def logout(response: Response):
    # remove auth cookies
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")
    return {"status": "success", "redirect": "/login"}

# Refresh endpoint (example)
@app.post("/refresh")
async def refresh_tokens(request: Request, response: Response):
    # read refresh token from httpOnly cookie
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token.")

    try:
        # call supabase to refresh (client shapes vary)
        try:
            refreshed = supabase.auth.refresh_session({"refresh_token": refresh_token})
        except AttributeError:
            # alternative client APIs
            try:
                refreshed = supabase.auth.refresh(refresh_token)
            except AttributeError:
                refreshed = supabase.auth.api.refresh_access_token(refresh_token)

        # normalize new tokens from possible shapes
        new_access = None
        new_refresh = None
        if isinstance(refreshed, dict):
            data = refreshed.get("data") or refreshed
            new_access = data.get("access_token")
            new_refresh = data.get("refresh_token")
        else:
            new_access = getattr(refreshed, "access_token", None) or (getattr(refreshed, "data", None) and getattr(refreshed.data, "access_token", None))
            new_refresh = getattr(refreshed, "refresh_token", None) or (getattr(refreshed, "data", None) and getattr(refreshed.data, "refresh_token", None))

        if not new_access:
            raise HTTPException(status_code=401, detail="Failed to refresh token.")

        # set new cookies
        response.set_cookie(key="access_token", value=new_access, httponly=True, secure=True, samesite="lax", max_age=15*60)  # example 15m
        if new_refresh:
            response.set_cookie(key="refresh_token", value=new_refresh, httponly=True, secure=True, samesite="lax", max_age=30*24*3600)

        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


# Example protected endpoint that validates cookie token
def get_current_user(access_token: Optional[str] = Cookie(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    # validate token with Supabase (method depends on client)
    try:
        user_resp = supabase.auth.get_user(access_token)
        user = getattr(user_resp, "user", None) or (user_resp.get("user") if isinstance(user_resp, dict) else None)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token.")
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token.")

# Retrieve profile
def get_profile(user_id: str):
    resp = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    if isinstance(resp, dict):
        return resp.get("data")
    return getattr(resp, "data", None)

# Dashboards
@app.get("/hr")
async def hr_dashboard(current_user = Depends(get_current_user)):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    profile = get_profile(user_id)
    if not profile or profile.get("role") != "employer":
        raise HTTPException(status_code=403, detail="Forbidden")
    # return whatever dashboard data you need
    return {"status": "success", "dashboard": "hr", "user_id": user_id, "profile": profile}

@app.get("/candidate")
async def candidate_dashboard(current_user = Depends(get_current_user)):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    profile = get_profile(user_id)
    if not profile or profile.get("role") != "employee":
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"status": "success", "dashboard": "candidate", "user_id": user_id, "profile": profile}

# Edit account
@app.get("/account/edit")
async def edit_account_page(current_user = Depends(get_current_user)):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    profile = get_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"status": "success", "profile": profile}

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {"user": {"id": current_user.id if hasattr(current_user, "id") else current_user.get("id"), "email": current_user.email if hasattr(current_user, "email") else current_user.get("email")}}
