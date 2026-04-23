import os
import json
import io
import fitz  # PyMuPDF: Install with 'pip install pymupdf'
from typing import Optional, List
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, Depends, Cookie, Request, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from openai import OpenAI
from fastapi.responses import JSONResponse, FileResponse

load_dotenv()

app = FastAPI()

@app.get("/")
async def read_index():
    # Looks for index.html in the same directory as main.py
    return FileResponse('login.html')

@app.get("/theme.js")
async def get_js():
    return FileResponse("theme.js", media_type="application/javascript")

@app.get("/design.css")
async def get_css():
    return FileResponse("design.css", media_type="text/css")

# --- CLIENTS ---
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
supabase_admin: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SERVICE_ROLE"))
zai_client = OpenAI(
    base_url="https://api.ilmu.ai/v1",
    api_key=os.getenv("Z_AI_API_KEY"),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500"                 # Local development
    ],
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
    company: Optional[str] = None  # Add this field
    remember: Optional[bool] = False

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    company: Optional[str] = None
    password: Optional[str] = None

class ApplicationData(BaseModel):
    full_name: str
    email: str
    role_title: str
    department: str
    skills: list
    form_details: dict

class StatusUpdate(BaseModel):
    status: str
    start_date: Optional[str] = None

# --- HELPERS ---
def get_current_user(access_token: Optional[str] = Cookie(None)):
    # If the cookie is missing, this triggers the 401
    if not access_token:
        print("DEBUG: access_token cookie is missing from the request!")
        raise HTTPException(status_code=401, detail="Auth session missing!")
    
    try:
        # Validate the token with Supabase
        user_resp = supabase.auth.get_user(access_token)
        user = getattr(user_resp, "user", None) or user_resp.get("user")
        
        if not user:
            raise HTTPException(status_code=401, detail="Invalid session.")
        return user
    except Exception as e:
        print(f"DEBUG: Token validation failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid session.")

def get_profile(user_id: str):
    try:
        resp = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        return getattr(resp, "data", None) or resp.get("data")
    except Exception as e:
        print(f"Database lookup failed for UUID {user_id}: {e}")
        return None

@app.post("/extract-cv")
async def extract_cv(file: UploadFile = File(...), current_user = Depends(get_current_user)):
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # 1. Extract Text from PDF
        file_content = await file.read()
        pdf_stream = io.BytesIO(file_content)
        doc = fitz.open(stream=pdf_stream, filetype="pdf")
        raw_text = "".join([page.get_text() for page in doc])
        doc.close()

        if not raw_text.strip():
            print(f"DEBUG [CV]: File {file.filename} contained no readable text.")
            raise HTTPException(status_code=400, detail="Could not extract text from PDF.")

        print(f"DEBUG [CV]: Extracting text from {file.filename} ({len(raw_text)} chars)...")

        # 2. AI Parsing Prompt
        system_instruction = (
            "You are a deterministic HR Data Parser. Extract info from the CV into a VALID JSON object.\n"
            "STRICT CATEGORY RULES:\n"
            "- department: [Engineering, Product, Design, HR, Finance, Marketing, Operations, Legal, Sales]\n"
            "- employment_type: [Full-time, Part-time, Contract, Internship, Freelance]\n"
            "- location_type: [Remote, Hybrid, On-site]\n"
            "Keys: full_name, email, role_title, department, employment_type, location_type, "
            "hiring_manager, years_experience, highest_qualification, previous_employer, skills (array).\n"
            "Use empty strings for unknowns. Return ONLY the JSON object."
        )

        completion = zai_client.chat.completions.create(
            model="ilmu-glm-5.1",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Parse this CV text: {raw_text[:4000]}"}
            ],
            response_format={ "type": "json_object" }
        )
        
        # 3. Robust JSON Parsing & Logging
        raw_ai_content = completion.choices[0].message.content
        print(f"\n[GLM RAW RESPONSE]\n{raw_ai_content}\n")

        # Clean AI response if it includes Markdown code blocks
        if "```json" in raw_ai_content:
            raw_ai_content = raw_ai_content.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_ai_content:
            raw_ai_content = raw_ai_content.split("```")[1].split("```")[0].strip()

        extracted_data = json.loads(raw_ai_content)
        print(f"DEBUG [CV]: Parsed successfully. Candidate: {extracted_data.get('full_name')}")

        # 4. Log the Extraction Event to Activity Logs
        await log_ai_event(
            user_id, 
            "cv_extraction", 
            f"AI parsed CV for {extracted_data.get('full_name', 'Unknown Candidate')}",
            {"filename": file.filename, "extracted_keys": list(extracted_data.keys())}
        )

        return {"status": "success", "extracted_data": extracted_data}

    except json.JSONDecodeError as je:
        print(f"CRITICAL [CV]: JSON Decode Error: {je}")
        raise HTTPException(status_code=500, detail="AI returned invalid JSON format.")
    except Exception as e:
        print(f"CRITICAL [CV]: Extraction Failure: {e}")
        raise HTTPException(status_code=500, detail=f"CV Parsing failed: {str(e)}")

# --- AUTH ENDPOINTS ---
@app.post("/signup")
async def signup(payload: SignupRequest):
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="Terms must be accepted.")
    
    try:
        auth_response = supabase.auth.sign_up({
            "email": payload.email, 
            "password": payload.password,
            "options": {"data": {"full_name": payload.full_name}} 
        })
        
        user = getattr(auth_response, "user", None) or (getattr(auth_response, "data", None) and getattr(auth_response.data, "user", None))
        if not user: raise Exception("Failed to create user.")
        
        user_id = getattr(user, "id", None)

        profile = {
            "id": user_id,
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "role": payload.role,
            "company": payload.company if payload.role == "employer" else None,
        }
        supabase.table("profiles").upsert(profile).execute()

        redirect = "/employerHome" if payload.role == "employer" else "/employeeHome"
        return {"status": "success", "redirect": redirect}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/login")
async def login(req: LoginRequest):
    try:
        # 1. First, attempt to sign in with Supabase Auth (Email/Password check)
        auth_response = supabase.auth.sign_in_with_password({
            "email": req.email, 
            "password": req.password
        })
        
        session = getattr(auth_response, 'session', None)
        if not session:
             raise HTTPException(status_code=401, detail="Invalid email or password.")

        # 2. Get the user profile to check Role and Company
        user_id = session.user.id
        profile = get_profile(user_id)
        
        if not profile:
            supabase.auth.sign_out()
            raise HTTPException(status_code=404, detail="User profile not found.")

        # 3. Verify Role
        if profile.get("role") != req.role:
            supabase.auth.sign_out()
            raise HTTPException(status_code=403, detail=f"This account is registered as an {profile.get('role')}.")

        # 4. Verify Company (Crucial Step for Employers)
        if req.role == "employer":
            # Strip whitespace and compare case-insensitively to be safe
            db_company = str(profile.get("company", "")).strip()
            provided_company = str(req.company).strip()
            
            if db_company != provided_company:
                supabase.auth.sign_out()
                # Printing this to terminal helps you debug the exact string mismatch
                print(f"DEBUG: DB Company '{db_company}' != Provided '{provided_company}'")
                raise HTTPException(status_code=401, detail="Company selection does not match your account.")

        # 5. Set Cookie and Redirect
        redirect = "employerHome" if req.role == "employer" else "employeeHome"
        res = JSONResponse(content={"status": "success", "redirect": redirect})
        
        # Cookie logic...
        res.set_cookie(key="access_token", value=session.access_token, httponly=True, samesite="lax")
        return res

    except Exception as e:
        # If Supabase Auth fails, it usually returns a JSON with 'msg'
        error_msg = str(e)
        if "Invalid login credentials" in error_msg:
            error_msg = "Invalid email or password."
        raise HTTPException(status_code=401, detail=error_msg)
    
@app.get("/companies")
async def get_companies():
    try:
        # 1. Fetch the company column from the profiles table
        resp = supabase.table("profiles") \
            .select("company") \
            .eq("role", "employer") \
            .execute()
        
        # 2. Extract the data
        data = resp.data or []
        
        # 3. Use a set to get UNIQUE names, and filter out None/empty strings
        unique_companies = sorted(list(set(
            item['company'] for item in data if item.get('company') and item['company'].strip()
        )))
        
        print(f"DEBUG: Found companies: {unique_companies}") # Check your terminal!
        return unique_companies
        
    except Exception as e:
        print(f"Error fetching companies: {e}")
        return []

@app.post("/logout")
async def logout():
    res = JSONResponse(content={"status": "success", "redirect": "/login"})
    res.delete_cookie("access_token", path="/")
    return res

# --- ACCOUNT MANAGEMENT ---
@app.get("/account-info")
async def get_account_info(current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    profile = get_profile(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found.")
    return profile

@app.patch("/applications/{app_id}/status")
async def update_app_status(app_id: str, payload: StatusUpdate, bg_tasks: BackgroundTasks, current_user = Depends(get_current_user)):
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # 1. Update the application status and start_date
        update_data = {"status": payload.status.lower()}
        if payload.start_date:
            update_data["start_date"] = payload.start_date

        # Update the application status record specifically for this employer
        # We use upsert to ensure a status row exists for the application/employer pair
        supabase.table("application_status").upsert({
            "application_id": app_id,
            "employer_id": user_id,
            "status": payload.status.lower(),
            "updated_at": datetime.now().isoformat()
        }, on_conflict="application_id,employer_id").execute()
        
        # 2. Also update the top-level start_date on the application if provided
        if payload.start_date:
            supabase.table("applications").update({"start_date": payload.start_date}).eq("id", app_id).execute()

        # 3. Log the event via AI
        bg_tasks.add_task(log_ai_event, user_id, "pipeline_move", f"Moved candidate to {payload.status}", {"app_id": app_id})
        
        return {"status": "success"}
    except Exception as e:
        print(f"Update Error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    
@app.get("/applications/{app_id}")
async def get_application_detail(app_id: str, current_user = Depends(get_current_user)):
    # Verify the application belongs to the requester
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    res = supabase.table("applications").select("*").eq("id", app_id).eq("candidate_id", user_id).single().execute()
    return res.data

@app.patch("/update-account")
async def update_account(
    payload: ProfileUpdate, 
    current_user = Depends(get_current_user),
    access_token: str = Cookie(None)  # <--- 1. We need the token here
):
    # Ensure user_id is a clean string
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # --- FIX 1: AUTHORIZE THE CLIENT ---
        # Without this, Supabase sees you as an "Anonymous" user.
        # If you have RLS enabled, it will silently fail to find the row.
        supabase.postgrest.auth(access_token) 

        # 1. Identity Changes (Password only)
        if payload.password and len(payload.password) >= 6:
            # Note: Identity updates use the Auth Admin logic
            supabase_admin.auth.admin.update_user_by_id(user_id, {"password": payload.password})

        # 2. Profile Changes (Name/Phone/Company)
        # We use exclude_unset so we don't overwrite fields with None
        profile_data = payload.dict(exclude_unset=True, exclude={"password"})
        
        if profile_data:
            # --- FIX 2: CHECK THE EXECUTION ---
            response = supabase.table("profiles").update(profile_data).eq("id", user_id).execute()
            
            # If response.data is empty, the 'id' didn't match any row
            if not response.data:
                print(f"DEBUG: No row found for ID {user_id}")
                raise Exception("Profile row not found in database.")

        return {"status": "success", "message": "Account fully updated"}

    except Exception as e:
        print(f"--- BACKEND ERROR ---\n{e}\n---------------------")
        raise HTTPException(status_code=400, detail=str(e))

# --- APPLICATIONS & DASHBOARD ---
@app.post("/applications")
async def submit_application(payload: ApplicationData, current_user = Depends(get_current_user)):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    prompt = f"Analyze candidate for {payload.role_title}. Skills: {payload.skills}"
    try:
        completion = zai_client.chat.completions.create(
            model="ilmu-glm-5.1",
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
    await log_ai_event(user_id, "application_submission", f"New application from {payload.full_name}", {"dept": payload.department})
    return {"status": "success"}

@app.get("/employee/applications")
async def get_employee_apps(current_user = Depends(get_current_user)):
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # Fetch applications belonging to this candidate
        # Also join with status to see what employers have set
        resp = supabase.table("applications")\
            .select("*, application_status(status, updated_at)")\
            .eq("candidate_id", user_id)\
            .order("created_at", desc=True)\
            .execute()
            
        return resp.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/hr/dashboard")
async def hr_dashboard(range_type: str = "4w", current_user = Depends(get_current_user)):
    # Ensure user_id is a string for consistent comparison
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # FIX: Remove the .eq filter from the main query to ensure all apps are fetched.
        # We filter by employer_id inside the application_status join instead.
        resp = supabase.table("applications")\
            .select("*, application_status!left(status, employer_id)")\
            .order("created_at", desc=True)\
            .execute()
        
        apps = []
        for a in resp.data or []:
            status_entries = a.get("application_status", [])
            
            # Filter the status entries to find the one belonging to THIS employer
            # If none exists, the candidate is "new" to this employer
            current_status = "new"
            if status_entries:
                # If it's a list (Supabase join return), find the matching employer_id
                if isinstance(status_entries, list):
                    match = next((s for s in status_entries if str(s.get('employer_id')) == user_id), None)
                    current_status = match.get("status", "new") if match else "new"
                else:
                    # If it's a single object
                    if str(status_entries.get('employer_id')) == user_id:
                        current_status = status_entries.get("status", "new")

            apps.append({
                **a, 
                "status": current_status, 
                "name": a.get("full_name") or "Unknown Candidate"
            })

        # --- 1. Pipeline & Metadata ---
        stages = ["New", "Reviewing", "Interview", "Offer", "Onboarding", "Rejected"]
        pipeline_counts = {stage: 0 for stage in stages}
        for a in apps:
            raw_status = a.get("status", "New").replace("_", " ").title()
            for s in stages:
                if s in raw_status:
                    pipeline_counts[s] += 1
                    break
        
        colors = {"New": "#4a9eff", "Reviewing": "#a855f7", "Interview": "#22c55e", "Offer": "#f59e0b", "Onboarding": "#4ade80", "Rejected": "#ef4444"}
        formatted_pipeline = [{"label": s, "count": pipeline_counts[s], "color": colors.get(s, "#6b7280")} for s in stages]

        # --- 2. Department Stats ---
        dept_map = {}
        for a in apps:
            d = a.get("department") or "General"
            dept_map[d] = dept_map.get(d, 0) + 1
        formatted_depts = [{"name": dept, "count": count} for dept, count in dept_map.items()]

        upcoming_starts = []
        for a in apps:
            # Look for the top-level 'start_date' column
            s_date = a.get("start_date")
            
            # Defensive check: if it's not at top-level, check if it's in the status join
            if not s_date:
                # Sometimes start_date is stored in the application_status table instead
                status_entries = a.get("application_status", [])
                if isinstance(status_entries, list) and status_entries:
                    s_date = status_entries[0].get("start_date")

            if s_date and a.get("status") != "rejected":
                print(f"DEBUG: Found start_date {s_date} for {a.get('full_name')}")
                upcoming_starts.append({
                    "id": a.get("id"), 
                    "full_name": a.get("full_name"), 
                    "role_title": a.get("role_title"), 
                    "start_date": s_date
                })

        # Deterministic sort
        upcoming_starts.sort(key=lambda x: str(x['start_date']))

        # --- 4. Dynamic Trend Logic ---
        today = datetime.now()
        trend_labels, trend_counts = [], []

        if range_type == "4w":
            for i in range(6, -1, -1):
                day_date = today - timedelta(days=i)
                trend_labels.append(day_date.strftime('%d %b'))
                comp_key = day_date.strftime('%Y-%m-%d')
                trend_counts.append(sum(1 for a in apps if a.get('created_at', '').split('T')[0] == comp_key))
        
        elif range_type == "3m":
            for i in range(11, -1, -1):
                start_week = today - timedelta(weeks=i+1)
                end_week = today - timedelta(weeks=i)
                trend_labels.append(f"Wk {12-i}")
                count = sum(1 for a in apps if start_week <= datetime.fromisoformat(a.get('created_at').replace('Z', '+00:00')).replace(tzinfo=None) < end_week)
                trend_counts.append(count)
                
        elif range_type == "6m":
            for i in range(5, -1, -1):
                month_date = today - timedelta(days=i*30)
                trend_labels.append(month_date.strftime('%b'))
                start_month = today - timedelta(days=(i+1)*30)
                end_month = today - timedelta(days=i*30)
                count = sum(1 for a in apps if start_month <= datetime.fromisoformat(a.get('created_at').replace('Z', '+00:00')).replace(tzinfo=None) < end_month)
                trend_counts.append(count)

        # --- 5. Return ---
        return {
            "recent_apps": apps[:10],
            "upcoming_starts": upcoming_starts[:5],
            "dept_stats": formatted_depts,
            "pipeline": formatted_pipeline,
            "glm_summary": f"GLM parsed {len(apps)*12} data points. Active pipeline: {sum(pipeline_counts.values()) - pipeline_counts.get('Rejected', 0)} candidates.",
            "stats": {
                "total_apps": len(apps), 
                "active_pipelines": sum(pipeline_counts.values()) - pipeline_counts.get("Rejected", 0),
                "interviews": pipeline_counts.get("Interview", 0),
                "pending": pipeline_counts.get("Reviewing", 0) + pipeline_counts.get("New", 0),
                "onboarding": pipeline_counts.get("Onboarding", 0), 
                "extractions": len(apps)*12
            },
            "trend": {"labels": trend_labels, "data": trend_counts}
        }
    except Exception as e:
        print(f"CRITICAL ERROR [HR_DASHBOARD]: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal dashboard processing error.")

# --- LOGGING & MONITORING ---
async def log_ai_event(user_id: str, event_type: str, raw_description: str, context_data: dict):
    clean_context = json.loads(json.dumps(context_data, default=str))
    system_instruction = "You are a deterministic System Log Analyst. Be concise (max 15 words). No conversational filler."
    analysis_prompt = f"Event: {raw_description}\nContext: {json.dumps(clean_context)}\nReturn JSON: {{'category': 'info|warning|error', 'ai_insight': 'string'}}"
    
    try:
        completion = zai_client.chat.completions.create(
            model="ilmu-glm-5.1",
            messages=[{"role": "system", "content": system_instruction}, {"role": "user", "content": analysis_prompt}],
            response_format={ "type": "json_object" }
        )
        analysis = json.loads(completion.choices[0].message.content)
    except:
        analysis = {"category": "info", "ai_insight": "System event logged."}

    supabase.table("activity_logs").insert({
        "user_id": str(user_id), "event_type": event_type, "description": raw_description,
        "category": analysis.get("category", "info"), "ai_note": analysis.get("ai_insight", ""), "metadata": clean_context 
    }).execute()

@app.get("/monitoring/logs")
async def get_monitoring_logs(current_user = Depends(get_current_user)):
    try:
        resp = supabase.table("activity_logs").select("*").order("created_at", desc=True).limit(20).execute()
        return resp.data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Log retrieval failed")

@app.get("/me")
async def me(current_user = Depends(get_current_user)):
    return {"id": getattr(current_user, "id", None), "email": getattr(current_user, "email", None)}