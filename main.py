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
from datetime import datetime, timedelta
from typing import List, Optional

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

# --- HR DASHBOARD ENDPOINT ---
@app.get("/hr/dashboard")
async def hr_dashboard(range_type: str = "4w", current_user = Depends(get_current_user)):
    """Aggregates real application data with daily vs monthly time-series bucketing."""
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    
    if not profile or profile.get("role") != "employer":
        raise HTTPException(status_code=403, detail="Access denied. Employer role required.")

    try:
        # 1. Fetch all applications
        resp = supabase.table("applications").select("*").order("created_at", desc=True).execute()
        apps = resp.data or []
        print(f"DEBUG: Found {len(apps)} applications in Supabase")

        # 2. Process Pipeline Stages (Case-Insensitive & Underscore Handling)
        stages = ["New", "Reviewing", "Interview", "Offer", "Onboarding", "Rejected"]
        pipeline_counts = {stage: 0 for stage in stages}
        for a in apps:
            # Map "offer_accepted" -> "Offer Accepted" then match against stage labels
            raw_status = a.get("status", "New").replace("_", " ").title()
            if raw_status in pipeline_counts:
                pipeline_counts[raw_status] += 1
            else:
                # Fallback: if it's "Offer Accepted", increment "Offer"
                for stage in stages:
                    if stage in raw_status:
                        pipeline_counts[stage] += 1
                        break
        
        colors = {
            "New": "#4a9eff", "Reviewing": "#a855f7", "Interview": "#22c55e",
            "Offer": "#f59e0b", "Onboarding": "#4ade80", "Rejected": "#ef4444"
        }
        formatted_pipeline = [
            {"label": s, "count": pipeline_counts[s], "color": colors.get(s, "#6b7280")} 
            for s in stages
        ]

        # 3. Process Department Breakdown
        dept_map = {}
        for a in apps:
            d = a.get("department", "Other")
            dept_map[d] = dept_map.get(d, 0) + 1
        
        formatted_depts = [
            {"name": dept, "count": count, "color": "#4a9eff"} 
            for dept, count in dept_map.items()
        ]

        # 4. DYNAMIC TREND LOGIC (Daily vs Monthly Aggregation)
        today = datetime.now()
        trend_labels = []
        trend_counts = []

        if range_type == "4w":
            # DAILY VIEW: Last 7 days
            for i in range(6, -1, -1):
                day_date = today - timedelta(days=i)
                day_label = day_date.strftime('%d %b')
                comparison_key = day_date.strftime('%Y-%m-%d')
                
                # Count apps where the YYYY-MM-DD matches
                count = sum(1 for a in apps if a.get('created_at', '').split('T')[0].split(' ')[0] == comparison_key)
                
                trend_labels.append(day_label)
                trend_counts.append(count)
        else:
            # MONTHLY VIEW: Last 3 or 6 months
            months_to_track = 3 if range_type == "3m" else 6
            
            for i in range(months_to_track - 1, -1, -1):
                # Calculate the target month by looking back blocks of ~30 days
                # This ensures we get the correct Month names
                target_date = today.replace(day=1) - timedelta(days=i*30)
                month_label = target_date.strftime('%b') # "Feb", "Mar", etc.
                month_key = target_date.strftime('%Y-%m') # "2026-02"
                
                # Count apps where the YYYY-MM matches the start of the timestamp
                count = sum(1 for a in apps if a.get('created_at', '').startswith(month_key))
                
                trend_labels.append(month_label)
                trend_counts.append(count)

        # 5. Build AI Insights
        insights = [
            {"label": "Volume", "icon": "▲", "text": f"Tracking {len(apps)} total applicants across {len(dept_map)} teams."}
        ]
        
        high_performers = [a for a in apps if (a.get("recommendation_rate") or 0) >= 80]
        if high_performers:
            insights.append({
                "label": "Top Talent", 
                "icon": "◈", 
                "text": f"{len(high_performers)} candidates have a recommendation rate over 80%."
            })

        # 5. Process Upcoming Starts (DYNAMIC)
        upcoming_starts = []
        today_date = datetime.now().date()

        for a in apps:
            # Normalize the status to lowercase for comparison
            status = (a.get("status") or "").lower().strip()
            start_date_str = a.get("start_date")
            
            # Match against your database values: 'onboarding' or 'offer_accepted'
            if status in ["onboarding", "offer_accepted"] and start_date_str:
                try:
                    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").date()
                    delta = (start_dt - today_date).days
                    
                    # Include if starting in the next 90 days
                    if delta >= 0:
                        upcoming_starts.append({
                            "name": a.get("full_name"),
                            "role": a.get("role_title") or "Position",
                            "dept": a.get("department") or "General",
                            "day": start_dt.day,
                            "month": start_dt.strftime('%b').upper(), # e.g. "APR"
                            "daysAway": delta,
                            "urgent": delta <= 7
                        })
                except Exception as e:
                    print(f"DEBUG: Date Parse Error for {a.get('full_name')}: {e}")

        # Sort soonest first
        upcoming_starts.sort(key=lambda x: x['daysAway'])

        # 6. Aggregated Return
        return {
            "recent_apps": apps[:10],
            "pipeline": formatted_pipeline,
            "dept_stats": formatted_depts,
            "insights": insights,
            "upcoming_starts": upcoming_starts, # NOW DYNAMIC
            "stats": {
                "total_apps": len(apps),
                "active_pipelines": sum(pipeline_counts[s] for s in stages if s != "Rejected"),
                "interviews": pipeline_counts.get("Interview", 0),
                "pending": pipeline_counts.get("Reviewing", 0),
                "onboarding": pipeline_counts.get("Onboarding", 0),
                "extractions": len(apps) * 4 
            },
            "trend": {
                "labels": trend_labels,
                "data": trend_counts
            }
        }

    except Exception as e:
        print(f"Dashboard Error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")

# --- HR PROFILE ENDPOINT (for the welcome message) ---
@app.get("/hr")
async def hr_info(current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    if not profile or profile.get("role") != "employer":
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"status": "success", "profile": profile}

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {
        "id": getattr(current_user, "id", None),
        "email": getattr(current_user, "email", None)
    }