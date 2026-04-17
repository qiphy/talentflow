import os
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from zai import ZaiClient
from fastapi.responses import JSONResponse # Added this for the fix

load_dotenv()

app = FastAPI()

# Clients
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
zai_client = ZaiClient(api_key=os.getenv("Z_AI_API_KEY"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500", "http://localhost:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"], 
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
        "company": payload.company if payload.role == "employer" else None,
    }
    supabase.table("profiles").insert(profile).execute()

# Signup endpoint
@app.post("/signup")
async def signup(payload: SignupRequest):
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="Terms must be accepted.")
    if payload.role not in ("employer", "employee"):
        raise HTTPException(status_code=400, detail="Invalid role.")

    try:
        try:
            auth_response = supabase.auth.sign_up({"email": payload.email, "password": payload.password})
        except AttributeError:
            auth_response = supabase.auth.sign_up(email=payload.email, password=payload.password)

        user = None
        if isinstance(auth_response, dict):
            user = auth_response.get("user") or (auth_response.get("data") and auth_response["data"].get("user"))
        else:
            user = getattr(auth_response, "user", None) or (getattr(auth_response, "data", None) and getattr(auth_response.data, "user", None))

        if not user:
            err = (auth_response.get("error") if isinstance(auth_response, dict) else getattr(auth_response, "error", None)) or "Failed to create user."
            raise Exception(err)

        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

        profile = {
            "id": user_id,
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "role": payload.role,
            "company": payload.company if payload.role == "employer" else None,
        }
        supabase.table("profiles").upsert(profile).execute()

        redirect = "/hr" if payload.role == "employer" else "/candidate"
        return {"status": "success", "message": "User created. Please log in.", "user_id": user_id, "role": payload.role, "redirect": redirect}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Login endpoint (FIXED with JSONResponse and secure=False)
@app.post("/login")
async def login(req: LoginRequest):
    try:
        # 1. Authenticate
        auth_response = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})

        # 2. Precise Extraction based on your structure
        # The structure you shared has .session and .user directly on the response
        session = getattr(auth_response, 'session', None)
        
        if not session:
             raise HTTPException(status_code=401, detail="Session not found in auth response.")

        access_token = session.access_token
        refresh_token = session.refresh_token
        user_id = session.user.id

        # 3. Role Lookup
        role = "employee"
        try:
            prof_resp = supabase.table("profiles").select("role").eq("id", user_id).execute()
            if prof_resp.data:
                role = prof_resp.data[0].get("role")
        except Exception as e:
            print(f"Role lookup warning: {e}")

        # 4. Create Response
        redirect = "/hrhome" if role == "employer" else "/candidatehome"
        res = JSONResponse(content={
            "status": "success",
            "user_id": user_id,
            "role": role,
            "redirect": redirect
        })

        # 5. Set Cookies (Critical settings for local dev)
        cookie_age = 7 * 24 * 3600 if req.remember else 3600
        
        res.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            secure=False, 
            samesite="lax",
            max_age=cookie_age,
            path="/"
        )

        if refresh_token:
            res.set_cookie(
                key="refresh_token",
                value=refresh_token,
                httponly=True,
                secure=False,
                samesite="lax",
                max_age=30 * 24 * 3600,
                path="/"
            )

        print(f"SUCCESS: Set-Cookie headers added for user {user_id}")
        return res

    except Exception as e:
        print(f"LOGIN ERROR: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))

# Logout (Updated for consistency)
@app.post("/logout")
async def logout():
    res = JSONResponse(content={"status": "success", "redirect": "/login"})
    res.delete_cookie("access_token", path="/")
    res.delete_cookie("refresh_token", path="/")
    return res

# Refresh (Updated for local dev security)
@app.post("/refresh")
async def refresh_tokens(request: Request):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token.")

    try:
        refreshed = supabase.auth.refresh_session({"refresh_token": refresh_token})
        new_access = getattr(refreshed, "access_token", None) or (getattr(refreshed, "data", None) and getattr(refreshed.data, "access_token", None))
        new_refresh = getattr(refreshed, "refresh_token", None) or (getattr(refreshed, "data", None) and getattr(refreshed.data, "refresh_token", None))

        res = JSONResponse(content={"status": "success"})
        res.set_cookie(key="access_token", value=new_access, httponly=True, secure=False, samesite="lax", max_age=15*60, path="/")
        if new_refresh:
            res.set_cookie(key="refresh_token", value=new_refresh, httponly=True, secure=False, samesite="lax", max_age=30*24*3600, path="/")
        return res
    except Exception:
        raise HTTPException(status_code=401, detail="Refresh failed")

# Protected Helpers
# This variable name 'access_token' must be identical to the cookie key!
def get_current_user(access_token: Optional[str] = Cookie(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    try:
        user_resp = supabase.auth.get_user(access_token)
        # Ensure we are returning the OBJECT, not just a string
        user = getattr(user_resp, "user", None) or user_resp.get("user")
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token.")
        return user # This should be the User object
    except Exception:
        # If you were doing 'return access_token' here, that's what caused the crash!
        raise HTTPException(status_code=401, detail="Invalid token.")

def get_profile(user_id: str):
    try:
        # We keep the query, but handle the potential exception
        resp = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        return getattr(resp, "data", None) or resp.get("data")
    except Exception as e:
        # This catches the PGRST116 '0 rows' error and returns None instead of crashing
        print(f"Database lookup failed for UUID {user_id}: {e}")
        return None

# Dashboards
@app.get("/hr")
async def hr_dashboard(current_user = Depends(get_current_user)):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    profile = get_profile(user_id)
    if not profile or profile.get("role") != "employer":
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"status": "success", "dashboard": "hr", "user_id": user_id, "profile": profile}

@app.get("/candidate")
async def candidate_dashboard(current_user = Depends(get_current_user)):
    user_id = current_user.id if hasattr(current_user, "id") else current_user.get("id")
    profile = get_profile(user_id)
    
    if not profile:
        # Instead of crashing, we give a clear message
        raise HTTPException(
            status_code=404, 
            detail=f"User authenticated but profile row missing for ID: {user_id}"
        )
    
    return {"status": "success", "profile": profile}

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {"user": {"id": current_user.id if hasattr(current_user, "id") else current_user.get("id"), "email": current_user.email if hasattr(current_user, "email") else current_user.get("email")}}