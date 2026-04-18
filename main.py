import os
import json
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from openai import OpenAI
from fastapi.responses import JSONResponse

load_dotenv()

app = FastAPI()

# --- CLIENTS ---
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
zai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("Z_AI_API_KEY"),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["set-cookie"],
)

# --- MODELS ---
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
    role: str
    remember: Optional[bool] = False

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None

class ApplicationData(BaseModel):
    full_name: str
    email: str
    role_title: str
    department: str
    skills: list
    form_details: dict

# --- HELPERS ---
def get_current_user(access_token: Optional[str] = Cookie(None)):
    if not access_token:
        raise HTTPException(status_code=401, detail="No access token cookie found.")
    try:
        user_resp = supabase.auth.get_user(access_token)
        user = getattr(user_resp, "user", None) or user_resp.get("user")
        if not user:
            raise HTTPException(status_code=401, detail="Invalid token.")
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token.")

def get_profile(user_id: str):
    try:
        resp = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        return getattr(resp, "data", None) or resp.get("data")
    except Exception as e:
        print(f"Database lookup failed for UUID {user_id}: {e}")
        return None

# --- AUTH ENDPOINTS ---
@app.post("/signup")
async def signup(payload: SignupRequest):
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="Terms must be accepted.")
    
    try:
        # 1. Create Auth User
        auth_response = supabase.auth.sign_up({
            "email": payload.email, 
            "password": payload.password,
            "options": {"data": {"full_name": payload.full_name}} # Sync to metadata
        })
        
        user = getattr(auth_response, "user", None) or (getattr(auth_response, "data", None) and getattr(auth_response.data, "user", None))
        if not user: raise Exception("Failed to create user.")
        
        user_id = getattr(user, "id", None)

        # 2. Create Profile Row
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
        return {"status": "success", "redirect": redirect}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/login")
async def login(req: LoginRequest):
    try:
        auth_response = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})
        session = getattr(auth_response, 'session', None)
        if not session:
             raise HTTPException(status_code=401, detail="Invalid credentials.")

        user_id = session.user.id
        
        # Verify Role
        profile = get_profile(user_id)
        if not profile or profile.get("role") != req.role:
            supabase.auth.sign_out()
            raise HTTPException(status_code=403, detail=f"Account is not registered as {req.role}.")

        redirect = "/employerHome" if profile.get("role") == "employer" else "/employeeHome"
        
        res = JSONResponse(content={"status": "success", "redirect": redirect})
        cookie_age = 7 * 24 * 3600 if req.remember else 3600
        
        res.set_cookie(key="access_token", value=session.access_token, httponly=True, secure=False, samesite="lax", max_age=cookie_age, path="/")
        if session.refresh_token:
            res.set_cookie(key="refresh_token", value=session.refresh_token, httponly=True, secure=False, samesite="lax", max_age=30*24*3600, path="/")
        return res

    except HTTPException as he: raise he
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@app.post("/logout")
async def logout():
    res = JSONResponse(content={"status": "success", "redirect": "/login"})
    res.delete_cookie("access_token", path="/")
    res.delete_cookie("refresh_token", path="/")
    return res

# --- ACCOUNT MANAGEMENT ---

@app.get("/account-info")
async def get_account_info(current_user = Depends(get_current_user)):
    """Fetches full profile data for the Account Settings page."""
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return profile

@app.patch("/update-account")
async def update_account(payload: ProfileUpdate, current_user = Depends(get_current_user)):
    """Updates specific profile fields."""
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    update_data = payload.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No changes provided.")

    try:
        supabase.table("profiles").update(update_data).eq("id", user_id).execute()
        return {"status": "success", "message": "Profile updated."}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- DASHBOARDS & APPLICATIONS ---

@app.get("/candidate")
async def candidate_dashboard(current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    if not profile or profile.get("role") != "employee":
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"status": "success", "profile": profile}

@app.post("/applications")
async def submit_application(payload: ApplicationData, current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    # AI Analysis Logic
    prompt = f"Analyze candidate for {payload.role_title}. Skills: {payload.skills}"
    try:
        completion = zai_client.chat.completions.create(
            model="z-ai/glm-4.5-air:free",
            messages=[{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        ai_data = json.loads(completion.choices[0].message.content)
        rec_rate, analysis_text = ai_data.get("score"), ai_data.get("justification")
    except:
        rec_rate, analysis_text = None, "AI Analysis unavailable."

    data = {
        "candidate_id": user_id,
        "full_name": payload.full_name,
        "email": payload.email,
        "role_title": payload.role_title,
        "department": payload.department,
        "skills": payload.skills,
        "form_details": payload.form_details,
        "recommendation_rate": rec_rate,
        "ai_analysis": analysis_text
    }

    supabase.table("applications").insert(data).execute()
    return {"status": "success"}

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {
        "id": getattr(current_user, "id", None),
        "email": getattr(current_user, "email", None)
    }