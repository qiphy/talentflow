import os
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from openai import OpenAI
from fastapi.responses import JSONResponse # Added this for the fix

load_dotenv()

app = FastAPI()

# Clients
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
# OpenRouter uses the OpenAI SDK structure
zai_client = OpenAI(
  base_url="https://openrouter.ai/api/v1",
  api_key=os.getenv("Z_AI_API_KEY"), # Your OpenRouter Key
)   

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5500"],
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
    role: str
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
        # 1. Authenticate with Supabase Auth
        auth_response = supabase.auth.sign_in_with_password({"email": req.email, "password": req.password})

        session = getattr(auth_response, 'session', None)
        if not session:
             raise HTTPException(status_code=401, detail="Invalid email or password.")

        access_token = session.access_token
        refresh_token = session.refresh_token
        user_id = session.user.id

        # 2. Role Lookup from the Profiles Table
        # We fetch the actual role assigned to this user ID
        try:
            prof_resp = supabase.table("profiles").select("role").eq("id", user_id).execute()
            if not prof_resp.data:
                raise HTTPException(status_code=404, detail="User profile not found.")
            
            db_role = prof_resp.data[0].get("role")
        except Exception as e:
            print(f"Database error: {e}")
            raise HTTPException(status_code=500, detail="Profile verification failed.")

        # 3. CRITICAL: Role Validation
        # Check if the role in the DB matches the portal the user selected
        if db_role != req.role:
            # Important: Sign out the session if roles don't match to maintain security
            supabase.auth.sign_out() 
            raise HTTPException(
                status_code=403, 
                detail=f"Role mismatch: This account is registered as an {db_role}."
            )

        # 4. Success: Define Redirects
        redirect = "/employerHome" if db_role == "employer" else "/employeeHome"
        
        res = JSONResponse(content={
            "status": "success",
            "user_id": user_id,
            "role": db_role,
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

        return res

    except HTTPException as he:
        # Re-raise HTTP exceptions so they return the correct status code to JS
        raise he
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
    print(f"DEBUG: Cookie 'access_token' received: {access_token is not None}")
    if not access_token:
        raise HTTPException(status_code=401, detail="No access token cookie found.")
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
    # 1. Robust ID Extraction
    # Handles User objects, dictionaries, or even a raw UUID string
    if isinstance(current_user, str):
        user_id = current_user
    else:
        user_id = getattr(current_user, "id", None) or current_user.get("id")

    # 2. Fetch the profile
    profile = get_profile(user_id)
    
    # 3. Validation Logic
    if not profile:
        print(f"ERROR: No profile row found for UUID: {user_id}")
        raise HTTPException(
            status_code=404, 
            detail="Profile not found. Please ensure your account setup is complete."
        )
    
    # Optional: Security check to ensure an employer can't access candidate dashboard
    if profile.get("role") != "employee":
        raise HTTPException(status_code=403, detail="Access denied: Not a candidate profile.")

    return {"status": "success", "profile": profile}

# Application Form (Candidate Page)
class ApplicationData(BaseModel):
    full_name: str
    email: str
    role_title: str
    department: str
    skills: list
    form_details: dict # This catches everything else

import json

@app.post("/applications")
async def submit_application(payload: ApplicationData, current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    # Initialize AI fields as None (NULL) so we can detect failures in HR dashboard
    rec_rate = None
    analysis_text = "Analysis pending (AI gateway timeout or error)."

    # 1. Prepare the prompt
    prompt = f"""
    Act as an expert technical recruiter. Analyze this candidate for the role: {payload.role_title}.
    Department: {payload.department}
    Skills: {', '.join(payload.skills)}
    Additional Info: {json.dumps(payload.form_details)}

    Return ONLY a raw JSON object:
    {{
      "score": (integer 0-100),
      "justification": (1-sentence explanation)
    }}
    """

    # 2. Call AI via OpenRouter (OpenAI-Compatible Syntax)
    try:
        # We use chat.completions.create because .generate() does not exist in the OpenAI SDK
        completion = zai_client.chat.completions.create(
            model="z-ai/glm-4.5-air:free", # Use the specific Z.AI model slug here
            messages=[{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" } # Forces valid JSON return
        )
        
        # Correct way to get the text response from an OpenAI-style client
        raw_ai_response = completion.choices[0].message.content
        
        # Parse the JSON string into a Python dictionary
        ai_data = json.loads(raw_ai_response)
        
        rec_rate = ai_data.get("score")
        analysis_text = ai_data.get("justification", "Analysis completed.")
        
    except Exception as e:
        # This keeps the submission alive even if the AI fails
        print(f"AI Analysis failed (Silent): {str(e)}")

    # 3. Final Data Assembly
    data = {
        "candidate_id": user_id,
        "full_name": payload.full_name,
        "email": payload.email,
        "role_title": payload.role_title,
        "department": payload.department,
        "skills": payload.skills,
        "form_details": payload.form_details,
        "recommendation_rate": rec_rate, # Will be NULL in DB if AI failed
        "ai_analysis": analysis_text
    }

    try:
        supabase.table("applications").insert(data).execute()
        return {"status": "success", "message": "Application received."}
    except Exception as e:
        print(f"SUPABASE ERROR: {str(e)}")
        raise HTTPException(status_code=400, detail="Database insertion failed.")

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {"user": {"id": current_user.id if hasattr(current_user, "id") else current_user.get("id"), "email": current_user.email if hasattr(current_user, "email") else current_user.get("email")}}