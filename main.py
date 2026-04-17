import os
import json
import fitz  # PyMuPDF
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from supabase import create_client, Client
from zai import ZaiClient # Official Z.ai SDK
from pydantic import BaseModel, EmailStr

load_dotenv()

app = FastAPI()

# 1. Setup Clients
supabase: Client = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
zai_client = ZaiClient(api_key=os.getenv("Z_AI_API_KEY"))

# 2. CORS for HTML/JS Frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

# 3. Submit Signup
class SignupRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str

@app.post("/signup")
async def signup(request: SignupRequest):
    try:
        # 1. Create the user in Supabase Auth
        # Supabase handles password hashing and security automatically
        auth_response = supabase.auth.sign_up({
            "email": request.email,
            "password": request.password,
        })

        if auth_response.user:
            # 2. Optional: Create a profile entry in your custom 'profiles' table
            # This is where you store non-auth data like their Full Name
            profile_data = {
                "id": auth_response.user.id, # Link to the Auth ID
                "full_name": request.full_name,
                "email": request.email,
                "role": "candidate" # or 'hr' depending on your logic
            }
            supabase.table("profiles").insert(profile_data).execute()

            return {
                "status": "success",
                "message": "User created. Please log in with your credentials.",
                "user_id": auth_response.user.id
            }
        
    except Exception as e:
        # Handle cases like "User already exists" or "Password too short"
        raise HTTPException(status_code=400, detail=str(e))