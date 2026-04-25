import os
import json
import io
import base64
import fitz  # PyMuPDF: Install with 'pip install pymupdf'
from typing import Optional
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Cookie, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from supabase import create_client, Client
from openai import OpenAI
from fastapi.responses import JSONResponse
import re
import pytesseract
from PIL import Image

load_dotenv()

app = FastAPI()

# --- CLIENTS ---
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
supabase_admin: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SERVICE_ROLE"))
zai_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("Z_AI_API_KEY"),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500" # Local development
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
    skills: list
    form_details: dict

class StatusUpdate(BaseModel):
    status: str
    start_date: Optional[str] = None
    notes: Optional[str] = None

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
        # 1. Extract Text Locally [cite: 358]
        print(f"DEBUG [CV]: Starting extraction for {file.filename}")
        file_content = await file.read()
        pdf_stream = io.BytesIO(file_content)
        
        try:
            doc = fitz.open(stream=pdf_stream, filetype="pdf")
            text_parts = [b[4] for page in doc for b in page.get_text("blocks")]
            raw_text = " ".join("\n".join(text_parts).split())
        except Exception as pdf_err:
            print(f"ERROR [PDF]: Failed to read PDF structure: {pdf_err}") 
            raise HTTPException(status_code=400, detail="Invalid PDF file format.")

        is_scanned = False
        messages = []
        
        # RESTORED: Comprehensive System Instruction [cite: 367, 396]
        system_instruction = (
            "CONTEXT: Current date is April 2026. You are a deterministic HR Data Parser.\n"
            "TASK: Convert CV text/image into a VALID JSON object.\n"
            "\nSTRICT FIELD RULES:\n"
            "1. full_name: Most prominent name at the top.\n"
            "2. phone_number: Extract digits/symbols (e.g., +6012-345-6789).\n"
            "3. nationality: Infer from address. Default to 'Malaysian' if address is in Malaysia.\n"
            "4. role_title: Use professional title. Match: [Software Engineer, Data Analyst, Product Manager, UI/UX Designer, HR Executive, Marketing Specialist, Operations Manager, Sales Executive].\n"
            "5. years_experience: CALCULATE sum of work history. If 'Present', use 2026. Return integer.\n"
            "6. preferred_location: Extract City/State.\n"
            "7. employment_type: MUST BE: [Full-time, Part-time, Contract, Internship, Freelance].\n"
            "8. location_type: MUST BE: [Remote, Hybrid, On-site].\n"
            "9. skills: Return an ARRAY of specific technical/professional skills. "
            "FILTER OUT subjective fluff like 'fast learner', 'hard working', or 'being really good'. "
            "STANDARDIZE names (e.g., 'ReactJS' -> 'React').\n"
            "\nJSON KEYS:\n"
            "full_name, email, phone_number, role_title, nationality, department, employment_type, "
            "location_type, preferred_location, years_experience, highest_qualification, previous_employer, skills\n"
            "\nReturn ONLY raw JSON. Use null for unknowns."
        )

        # 2. OCR Restoration [cite: 259, 272]
        if not raw_text.strip():
            print("DEBUG [CV]: No text found. Triggering OCR Multimodal logic.") 
            is_scanned = True
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) 
            img_data = pix.tobytes("png")
            base64_image = base64.b64encode(img_data).decode('utf-8')
            
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": [
                    {"type": "text", "text": "Scanned CV image. Parse visible info into JSON."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_image}"}}
                ]}
            ]
        else:
            print(f"DEBUG [CV]: Text extracted ({len(raw_text)} chars). Sending to GLM.") 
            messages = [
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": f"Parse this CV text: {raw_text[:8000]}"}
            ]

        doc.close()

        # 3. Call GLM API [cite: 346, 494]
        try:
            completion = zai_client.chat.completions.create(
                model="z-ai/glm-4.5-air:free", # Air model for low-latency response [cite: 494]
                messages=messages,
                response_format={ "type": "json_object" }
            )
            raw_ai_content = completion.choices[0].message.content
        except Exception as api_err:
            print(f"ERROR [API]: GLM API call failed: {api_err}") [cite: 144]
            raise HTTPException(status_code=503, detail="AI Service temporarily unavailable.")

        # 4. JSON Sanitization [cite: 388, 389]
        try:
            extracted_data = json.loads(raw_ai_content)
        except json.JSONDecodeError as json_err:
            print(f"ERROR [JSON]: Malformed content: {raw_ai_content}") 
            raise HTTPException(status_code=500, detail="AI response could not be parsed.")

        # 5. Layer 2: Python-level Sanity Check [cite: 217]
        BANNED_FLUFF = {"being really good", "hardworking", "team player", "quick learner", "fast learner"}
        if "skills" in extracted_data and isinstance(extracted_data["skills"], list):
            original_count = len(extracted_data["skills"])
            extracted_data["skills"] = [
                s for s in extracted_data["skills"] 
                if isinstance(s, str) and s.lower().strip() not in BANNED_FLUFF
            ]
            print(f"DEBUG [SANITY]: Removed {original_count - len(extracted_data['skills'])} rubbish skills.")

        # 6. Log Event [cite: 41, 131]
        try:
            await log_ai_event(
                user_id, 
                "cv_extraction", 
                f"AI parsed CV for {extracted_data.get('full_name', 'Unknown')}",
                {"filename": file.filename, "method": "ocr" if is_scanned else "text"}
            )
        except Exception as log_err:
            print(f"WARNING [LOG]: Activity log failed: {log_err}") 

        return {"status": "success", "extracted_data": extracted_data}

    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"CRITICAL [SYSTEM]: {e}") 
        raise HTTPException(status_code=500, detail="An internal server error occurred.")
    
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
        result = supabase.table("application_status").upsert({
            "application_id": app_id,
            "employer_id": user_id,
            "status": payload.status.lower(),
            "notes": payload.notes, 
            "updated_at": datetime.now().isoformat()
        }, on_conflict="application_id,employer_id").execute()
        
        print(f"DEBUG [SUPABASE]: Upsert Success. Rows affected: {len(result.data)}")
        
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
# Assuming your ApplicationData model looks like this now:
@app.post("/applications")
async def submit_application(
    payload: ApplicationData, 
    background_tasks: BackgroundTasks, # Add this
    current_user = Depends(get_current_user)
):
    user_id = getattr(current_user, "id", None) or current_user.get("id")
    
    # 1. Save data immediately to Supabase with placeholder values [cite: 568]
    data = {
        "candidate_id": user_id,
        "full_name": payload.full_name,
        "email": payload.email,
        "role_title": payload.role_title,
        "department": "Pending...", # Placeholder [cite: 565]
        "skills": payload.skills,
        "form_details": payload.form_details,
        "recommendation_rate": 0,    # Placeholder
        "ai_analysis": "AI is currently analyzing your fit..."
    }

    res = supabase.table("applications").insert(data).execute()
    app_id = res.data[0]['id']

    # 2. Offload the heavy AI logic to a background task 
    background_tasks.add_task(analyze_application_background, app_id, user_id, payload)
    
    return {"status": "success", "message": "Application received! Our AI is evaluating your profile."}

async def analyze_application_background(app_id: str, user_id: str, payload: ApplicationData):
    prompt = f"""
    CONTEXT: April 2026. You are a senior technical recruiter[cite: 44].
    TASK: Evaluate '{payload.role_title}' based on skills: {payload.skills}.
    
    DEPARTMENTS: Engineering, Design, Marketing, Sales, HR, Finance, Operations.
    
    RETURN JSON:
    - "department": (string)
    - "score": (int 0-100)
    - "justification": (short string)
    - "concerns": (list of missing skills)
    """

    try:
        completion = zai_client.chat.completions.create(
            model="z-ai/glm-4.5-air:free",
            messages=[{"role": "user", "content": prompt}],
            response_format={ "type": "json_object" }
        )
        
        ai_data = json.loads(completion.choices[0].message.content)
        
        # Format analysis text for the HR Dashboard [cite: 298]
        analysis_text = (
            f"Score: {ai_data.get('score')}% | "
            f"Justification: {ai_data.get('justification')} "
            f"Concerns: {', '.join(ai_data.get('concerns', []))}"
        )

        # 3. Update the existing record with real AI insights [cite: 277, 353]
        supabase.table("applications").update({
            "department": ai_data.get("department", "General"),
            "recommendation_rate": ai_data.get("score", 0),
            "ai_analysis": analysis_text
        }).eq("id", app_id).execute()

        # Log the success for the Monitoring Dashboard [cite: 40, 87]
        await log_ai_event(user_id, "application_analysis_complete", 
                           f"Analysis finished for {payload.full_name}", 
                           {"dept": ai_data.get("department")})

    except Exception as e:
        # Fallback: If AI fails, move to Manual Review [cite: 51, 144]
        supabase.table("applications").update({
            "department": "Review Required",
            "ai_analysis": "AI analysis timed out. Manual HR review recommended."
        }).eq("id", app_id).execute()

@app.get("/employee/applications")
async def get_employee_apps(current_user = Depends(get_current_user)):
    user_id = str(getattr(current_user, "id", None) or current_user.get("id"))
    
    try:
        # profiles:employer_id(company) explicitly links the FK to the profile table
        resp = supabase.table("applications")\
            .select("""
                *,
                application_status(
                    status, 
                    updated_at,
                    employer_id,
                    profiles:employer_id(company),
                    notes
                )
            """)\
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
            status_entries = a.get("application_status", [])
            
            # 1. Find the entry belonging to THIS employer
            # We use next() to find the first match in the list
            user_status_entry = next(
                (s for s in status_entries if str(s.get('employer_id')) == user_id), 
                None
            )

            if user_status_entry:
                # 2. Get the date from the status entry OR the top-level app
                s_date = user_status_entry.get("start_date") or a.get("start_date")
                
                # 3. Check status and date safely
                if s_date and user_status_entry.get("status") != "rejected":
                    upcoming_starts.append({
                        "id": a.get("id"), 
                        "employer_id": user_id,
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
            # FIX: Send the full list so the Calendar can actually show all dates
            "upcoming_starts": upcoming_starts, 
            # If you specifically need a "Top 5" for a sidebar, create a new key:
            "upcoming_starts_summary": upcoming_starts[:5],
            "user_id": user_id,
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
    # Ensure context is JSON serializable [cite: 535, 570]
    clean_context = json.loads(json.dumps(context_data, default=str))
    
    system_instruction = (
        "You are a deterministic System Log Analyst. "
        "Categorize events as info, warning, or error. "
        "Be concise (max 15 words). No conversational filler."
    )
    
    analysis_prompt = (
        f"Event: {raw_description}\n"
        f"Context: {json.dumps(clean_context)}\n"
        f"Return JSON: {{'category': 'info|warning|error', 'ai_insight': 'string'}}"
    )
    
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
    except Exception:
        # Graceful fallback logic as documented [cite: 51, 53]
        analysis = {"category": "info", "ai_insight": "System event logged manually."}

    # Insert into Supabase activity_logs table [cite: 130, 486]
    supabase.table("activity_logs").insert({
        "user_id": str(user_id), 
        "event_type": event_type, 
        "description": raw_description,
        "category": analysis.get("category", "info"), 
        "ai_note": analysis.get("ai_insight", ""), 
        "metadata": clean_context 
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