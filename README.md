# TalentFlow

![Frontend](https://img.shields.io/badge/Frontend-HTML-E34F26?logo=html5&logoColor=white)
![Styles](https://img.shields.io/badge/Styles-CSS-1572B6?logo=css3&logoColor=white)
![Scripts](https://img.shields.io/badge/Scripts-JavaScript-F7DF1E?logo=javascript&logoColor=black)
![Backend](https://img.shields.io/badge/Backend-Python-3776AB?logo=python&logoColor=white)
![Database](https://img.shields.io/badge/Database-Supabase-3FCF8E?logo=supabase&logoColor=white)

---

## Overview

A web-based talent management and recruitment platform that connects employers and job candidates through an AI-powered application review system. TalentFlow provides separate experiences for two user roles:

- **Employers** — post roles, review incoming applications, and receive AI-generated candidate scores and analysis to assist hiring decisions.
- **Employees / Candidates** — browse opportunities, submit applications, and track their application status through a personal dashboard.

An AI model (GLM via OpenRouter) automatically analyses each submitted application and returns a recommendation score and justification, visible to employers on the monitoring dashboard.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI, Uvicorn |
| Database & Auth | Supabase (PostgreSQL + Supabase Auth) |
| AI Analysis | OpenRouter API (GLM-4.5-air model) |
| Frontend | HTML, CSS, JavaScript |

---

## Project Structure

```
talentflow-main/
├── main.py               # FastAPI backend — all API routes
├── requirements.txt      # Python dependencies
├── .env                  # Environment variables (see setup below)
├── design.css            # Shared design system / CSS variables
├── navbar.css            # Navigation bar styles
├── theme.js              # Light/dark theme toggle logic
├── login.html            # Login page (employer & employee)
├── signup.html           # Signup page
├── employerHome.html     # Employer dashboard
├── employeeHome.html     # Employee/candidate dashboard
├── employerAcc.html      # Employer account settings
├── employeeAcc.html      # Employee account settings
├── candidate.html        # Candidate profile / application view
├── monitoring.html       # AI monitoring dashboard (GLM)
└── template.html         # Base HTML template
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- A [Supabase](https://supabase.com) project with a `profiles` table and an `applications` table
- An API key
- A local static file server (e.g. VS Code Live Server, `npx serve`, or similar)

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd talentflow-main
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the project root with the following keys:

```env
SUPABASE_URL=your_supabase_project_url
SUPABASE_KEY=your_supabase_anon_or_service_key
Z_AI_API_KEY=your_openrouter_api_key
SERVICE_ROLE=your_supabase_service_role_jwt
```

> ⚠️ **Never commit your `.env` file to version control.** Add it to `.gitignore`.

### 4. Set up Supabase tables

In your Supabase project, create the following tables:

**`profiles`**
| Column | Type |
|---|---|
| id | uuid (primary key, references auth.users) |
| full_name | text |
| email | text |
| phone | text |
| role | text (`employer` or `employee`) |
| company | text (nullable) |

**`applications`**
| Column | Type |
|---|---|
| id | uuid (auto-generated) |
| candidate_id | uuid |
| full_name | text |
| email | text |
| role_title | text |
| department | text |
| skills | jsonb |
| form_details | jsonb |
| recommendation_rate | numeric (nullable) |
| ai_analysis | text (nullable) |

### 5. Start the backend

```bash
uvicorn main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

### 6. Serve the frontend

Open the HTML files using a local static server listening on `http://127.0.0.1:5500` (e.g. VS Code Live Server). This origin is whitelisted in the CORS configuration.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/signup` | Register a new employer or employee account |
| `POST` | `/login` | Authenticate and receive a session cookie |
| `POST` | `/logout` | Clear session cookies |
| `GET` | `/me` | Return the current authenticated user's ID and email |
| `GET` | `/account-info` | Fetch the current user's full profile |
| `PATCH` | `/update-account` | Update name or phone number |
| `GET` | `/candidate` | Load the candidate dashboard (employee role only) |
| `POST` | `/applications` | Submit a job application (triggers AI analysis) |

All protected endpoints require a valid `access_token` cookie set at login.

---

## Authentication

TalentFlow uses Supabase Auth with HTTP-only session cookies. On login, the server sets an `access_token` cookie (1 hour by default, 7 days if "Remember me" is checked) and a `refresh_token` cookie (30 days). Role verification is enforced server-side — an employer cannot log in via the employee portal and vice versa.

---

## Notes

- The frontend is currently configured to communicate with the backend at `http://127.0.0.1:8000`. Update the fetch URLs in the HTML files if deploying to a different host.
- CORS is restricted to `http://127.0.0.1:5500`. Update the `allow_origins` list in `main.py` before deploying to production.
- Cookies are set with `secure=False` for local development. Set `secure=True` when serving over HTTPS in production.
