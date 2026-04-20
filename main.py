import os
import json
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request, BackgroundTasks
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

    await log_ai_event(
        user_id=user_id,
        event_type="application_submission",
        raw_description=f"New application from {payload.full_name} for {payload.role_title}",
        context_data={"dept": payload.department, "skills": payload.skills}
    )

    return {"status": "success"}

# --- ADD THIS MODEL ---
class StatusUpdate(BaseModel):
    status: str
    start_date: Optional[str] = None

@app.patch("/applications/{app_id}/status")
async def update_app_status(
    app_id: str, 
    payload: StatusUpdate, 
    bg_tasks: BackgroundTasks, # Add this parameter
    current_user = Depends(get_current_user)
):
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # 1. IMMEDIATE ACTION: Update the status table
        # We do this first so the UI refreshes accurately
        status_data = {
            "application_id": app_id,
            "employer_id": user_id,
            "status": payload.status.lower(),
            "updated_at": datetime.now().isoformat()
        }

        supabase.table("application_status").upsert(
            status_data, 
            on_conflict="application_id,employer_id" 
        ).execute()

        # 2. BACKGROUND ACTION: AI Processing & Logging
        # This will run after the response is sent to the user
        bg_tasks.add_task(process_ai_logs, user_id, app_id, payload.status)

        if payload.status.lower() == "onboarding" and payload.start_date:
            supabase.table("applications").update({"start_date": payload.start_date}).eq("id", app_id).execute()

        # 3. RETURN INSTANTLY
        return {"status": "success"}

    except Exception as e:
        print(f"PATCH Error Detail: {str(e)}") 
        raise HTTPException(status_code=400, detail=str(e))

# Helper function for background processing
async def process_ai_logs(user_id: str, app_id: str, new_status: str):
    try:
        if new_status.lower() == "onboarding":
            cand_resp = supabase.table("applications").select("form_details, full_name").eq("id", app_id).single().execute()
            candidate = cand_resp.data
            
            # Check if 'date' exists inside the form_details JSONB
            form_details = candidate.get("form_details") or {}
            start_date = form_details.get("date")
            
            if not start_date:
                name = candidate.get("full_name", "Unknown")
                await log_ai_event(
                    user_id, 
                    "logic_conflict", 
                    f"Onboarding started for {name} without a start date in form details.",
                    {"severity": "warning", "issue": "NULL_START_DATE", "app_id": app_id}
                )

        # Standard Activity Log
        await log_ai_event(
            user_id, 
            "pipeline_move", 
            f"Candidate status updated to {new_status}",
            {"app_id": app_id, "new_status": new_status}
        )
    except Exception as e:
        print(f"Background Logging Error: {e}")

# --- UPDATED: HR Dashboard (Now joins with application_status) ---
@app.get("/hr/dashboard")
async def hr_dashboard(range_type: str = "4w", current_user = Depends(get_current_user)):
    """Aggregates isolated application data for the specific employer."""
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    
    if not profile or profile.get("role") != "employer":
        raise HTTPException(status_code=403, detail="Access denied. Employer role required.")

    try:
        # 1. Fetch applications with an isolated join
        resp = supabase.table("applications") \
            .select("*, application_status!left(status)") \
            .eq("application_status.employer_id", user_id) \
            .order("created_at", desc=True) \
            .execute()
        
        raw_apps = resp.data or []
        apps = []

        for a in raw_apps:
            status_entries = a.get("application_status", [])
            current_status = status_entries[0].get("status", "new") if status_entries else "new"
            
            app_item = {
                "id": a.get("id"),
                "full_name": a.get("full_name"),
                "name": a.get("full_name"),
                "role_title": a.get("role_title"),
                "department": a.get("department"),
                "status": current_status,
                "start_date": a.get("start_date"),
                "created_at": a.get("created_at"),
                "recommendation_rate": a.get("recommendation_rate", 0),
            }
            apps.append(app_item)

        # --- 2. Process Pipeline Stages (RESTORED COLORS) ---
        stages = ["New", "Reviewing", "Interview", "Offer", "Onboarding", "Rejected"]
        pipeline_counts = {stage: 0 for stage in stages}
        
        for a in apps:
            raw_status = a.get("status", "New").replace("_", " ").title()
            for s in stages:
                if s in raw_status:
                    pipeline_counts[s] += 1
                    break
        
        # Your original specific color mapping
        colors = {
            "New": "#4a9eff", "Reviewing": "#a855f7", "Interview": "#22c55e",
            "Offer": "#f59e0b", "Onboarding": "#4ade80", "Rejected": "#ef4444"
        }
        
        formatted_pipeline = [
            {"label": s, "count": pipeline_counts[s], "color": colors.get(s, "#6b7280")} 
            for s in stages
        ]

        # 3. Department Stats
        dept_map = {}
        for a in apps:
            d = a.get("department", "Other")
            dept_map[d] = dept_map.get(d, 0) + 1
        formatted_depts = [{"name": dept, "count": count} for dept, count in dept_map.items()]

        # 4. Metrics
        onboarded_apps = [a for a in apps if a['status'] == 'onboarding']
        avg_time = "0 Days"
        if onboarded_apps:
            total_days = 0
            for a in onboarded_apps:
                try:
                    start = datetime.fromisoformat(a['created_at'].replace('Z', '+00:00'))
                    total_days += max((datetime.now(start.tzinfo) - start).days, 0)
                except: continue
            avg_time = f"{round(total_days / len(onboarded_apps))} Days"

        offers_sent = pipeline_counts.get("Offer", 0) + pipeline_counts.get("Onboarding", 0)
        acceptance_rate = f"{round((pipeline_counts.get('Onboarding', 0)/offers_sent)*100)}%" if offers_sent > 0 else "0%"

        # 5. Trend (FIXED: This now populates the chart)
        today = datetime.now()
        trend_labels, trend_counts = [], []
        for i in range(6, -1, -1):
            day_date = today - timedelta(days=i)
            day_label = day_date.strftime('%d %b')
            comp_key = day_date.strftime('%Y-%m-%d')
            count = sum(1 for a in apps if a.get('created_at', '').split('T')[0] == comp_key)
            trend_labels.append(day_label)
            trend_counts.append(count)

        # 6. Upcoming Starts
        upcoming_starts = [{"id": a["id"], "name": a["name"], "role": a["role_title"], "start_date": a["start_date"]} 
                           for a in apps if a.get("status") == "onboarding" and a.get("start_date")]
        upcoming_starts.sort(key=lambda x: x['start_date'])

        # 7. GLM Summary (Linking the AI Strip)
        top_dept = formatted_depts[0]['name'] if formatted_depts else "N/A"
        live_summary = f"GLM has extracted {len(apps) * 12} data points. Top hiring focus is {top_dept}. Velocity: {avg_time}."

        return {
            "recent_apps": apps[:10],
            "upcoming_starts": upcoming_starts[:5],
            "pipeline": formatted_pipeline,
            "dept_stats": formatted_depts,
            "glm_summary": live_summary,
            "stats": {
                "total_apps": len(apps),
                "active_pipelines": sum(pipeline_counts[s] for s in stages if s != "Rejected"),
                "interviews": pipeline_counts.get("Interview", 0),
                "pending": pipeline_counts.get("Reviewing", 0) + pipeline_counts.get("New", 0),
                "onboarding": pipeline_counts.get("Onboarding", 0),
                "extractions": len(apps) * 12,
                "avg_time_to_hire": avg_time,
                "offer_acceptance": acceptance_rate
            },
            "trend": {
                "labels": trend_labels,
                "data": trend_counts
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# -- AI Logging -- #    
async def log_ai_event(user_id: str, event_type: str, raw_description: str, context_data: dict):
    # 1. Clean the context data
    clean_context = json.loads(json.dumps(context_data, default=str))

    # SYSTEM PROMPT: Sets the persona and strict rules
    system_instruction = (
        "You are a deterministic System Log Analyst. Analyze events for an HR platform. "
        "Rules: 1. Be extremely concise (max 15 words). 2. No conversational filler or 'This is a...'. "
        "3. Use technical language. 4. If an anomaly exists, flag the specific missing variable."
    )

    # USER PROMPT: Provides the specific data
    analysis_prompt = f"""
    Event: {raw_description}
    Context: {json.dumps(clean_context)}
    
    Task: Categorize and provide a sharp technical insight.
    Return JSON format: {{"category": "info|warning|error", "ai_insight": "string"}}
    """
    
    try:
        completion = zai_client.chat.completions.create(
            model="z-ai/glm-4.5-air:free",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": analysis_prompt}
            ],
            response_format={ "type": "json_object" }
        )
        analysis = json.loads(completion.choices[0].message.content)
    except Exception as e:
        print(f"AI Logging Inference Failed: {e}")
        analysis = {"category": "info", "ai_insight": "Standard system event log recorded."}

    # 2. Database Insert
    try:
        result = supabase.table("activity_logs").insert({
            "user_id": str(user_id), 
            "event_type": event_type,
            "description": raw_description,
            "category": analysis.get("category", "info"),
            "ai_note": analysis.get("ai_insight", ""),
            "metadata": clean_context 
        }).execute()
        return result
    except Exception as e:
        print(f"CRITICAL DATABASE ERROR: {e}")
        raise e

# -- Getting Monitoring Logs -- #
@app.get("/monitoring/logs")
async def get_monitoring_logs(current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    try:
        # Fetching logs
        resp = supabase.table("activity_logs").select("*").order("created_at", desc=True).limit(20).execute()
        
        # DEBUG: Check your terminal! If this prints [], your table is empty.
        print(f"DEBUG: Found {len(resp.data)} logs for monitoring.") 
        
        return resp.data
    except Exception as e:
        print(f"ERROR: {e}")
        raise HTTPException(status_code=500, detail="Database connection failed")

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