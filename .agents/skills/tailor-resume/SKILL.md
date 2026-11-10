---
name: tailor-resume
description: >-
  Automatically fetches jobs marked 'Want to Apply' from the user's Notion board,
  checks data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/ folders to skip postings that
  already have an EngelhardtJoshuaResume.docx file, loads the generated Argon prompt
  packets for new postings and the user's master .docx resume from data/, and generates
  tailored resume summaries, bullet rewrites, ATS keyword alignments, and tailored
  EngelhardtJoshuaResume.docx and EngelhardtJoshuaCV.docx files grouped by business
  and requisition number inside data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/.
  Activate this skill whenever the user asks to tailor their resume, prepare an
  application, or process their Notion 'Want to Apply' queue.
---

# Automated Notion 'Want to Apply' Resume Tailoring Skill

Use this skill whenever the user asks to tailor their resume for jobs in their Notion **`Want to Apply`** queue (or for a specific company/posting). You do **not** need the user to copy-paste the prompt manually—follow the automated steps below.

## Step-by-Step Procedure

1. **Refresh the Notion `'Want to Apply'` Queue & Check for Existing Business / Position Folders (with Requisition Numbers):**
   - First, inspect `data/tailored/` to see which business/position subfolders **already contain `EngelhardtJoshuaResume.docx`** (grouped by business with requisition number: `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/EngelhardtJoshuaResume.docx`).
   - Run the queue processor:
     ```powershell
     C:\Users\jenge\anaconda3\envs\job-matcher\python.exe -m src.exporters.resume_tailor
     ```
     Note: `process_want_to_apply_queue()` automatically checks `find_existing_tailored_resume(company, title, req_id=..., external_id=..., url=...)` across `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/` subfolders and **skips** any `'Want to Apply'` card that already has `EngelhardtJoshuaResume.docx` inside its folder unless the user explicitly asks to tailor/regenerate that specific posting.
   - If the user explicitly names a posting that was already skipped (or asks to redo it), you can read the job's details directly from MySQL or run `process_want_to_apply_queue(..., force_regenerate=True)`.

2. **Never Recreate an Existing Tailored Resume in Batch Mode Unless Explicitly Asked:**
   - In general batch mode (*"tailor my resume"*), if `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/EngelhardtJoshuaResume.docx` **already exists** for a `Company` + `Title` + `Req_ID`, **skip recreating it** and report:
     `[SKIP] <Company> — <Title> (<Req_ID>) (already tailored: data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/EngelhardtJoshuaResume.docx)`
   - Only process the **new / untailored** postings from the `'Want to Apply'` queue (or the specific posting the user asked for).

3. **Preview Changes First & Enforce Strict Closed-World Skills for ALL Resumes in the Queue (Mandatory Human-in-the-Loop Approval):**
   - **STRICT CLOSED-WORLD SKILL RULE (NO UNAPPROVED SKILLS IN RESUME OR COVER LETTER):**
     - You must **ONLY** use skills, tools, frameworks, and methodologies that **already exist on the user's Master Resume** when drafting the Tailored Summary, Technical Skills, Experience/Project Bullets, and Cover Letter (`EngelhardtJoshuaCV.docx`).
     - **NEVER** sneak new skills, tools, or methods from the job description (or from missing ATS skill groups) into the resume or cover letter draft unless the user has explicitly confirmed they have done them.
     - Instead, collect all missing/suggested skills from each job posting into **one single, dedicated section** (`### ❓ Candidate Skill Check — Have You Done Any of These?`) so the user can review them in one place and tell you which ones (if any) they have actually done.
   - **MANDATORY PREVIEW & APPROVAL FOR ALL QUEUED RESUMES (NO EXCEPTIONS IN BATCH MODE):**
     - Whether the user asks to tailor a single specific posting **OR** asks to tailor all jobs in the Notion **`Want to Apply`** / **`Ready to Apply`** queue in batch mode, **NEVER write any `.docx` files or create any position folders immediately!**
     - You **MUST** present a clear **Before $\rightarrow$ After Proposed Changes Preview** in chat for **EVERY** untailored job in the queue first. For each job in the queue, show:
       1. **Tailored Professional Summary:** Show **Current Summary** vs. **Proposed Tailored Summary (3–4 lines)** using **only** skills and experience already present on the master resume, framed to match the role's focus.
       2. **Technical Skills Adjustments (Reordering / Emphasis Only):** Show **Current Line(s)** vs. **Proposed Line(s)** reordering or emphasizing existing master-resume skills to match the posting—with **zero unverified new skills added**.
       3. **Experience & Project Bullet Rewrites:** For each bullet you plan to modify, show **Original Bullet** $\rightarrow$ **Proposed Rewritten Bullet** (`Action Verb + Technical Method/Tool + Quantified Impact`) using **only** tools and work already on the master resume.
       4. **❓ Candidate Skill Check — Have You Done Any of These? (Consolidated Checklist):** List all high-value skills/tools from the job description(s) that are **not** currently on the master resume in a clear checklist, asking the user if they have experience with any of them and want them added to the corresponding resume(s).
       5. **Cover Letter Preview (`EngelhardtJoshuaCV.docx`):** Show the concise 3-paragraph cover letter text that will be saved to `EngelhardtJoshuaCV.docx` for each job.
     - Immediately delete any temporary `.txt` or `.md` prompt files generated during Step 1 (`Remove-Item data\tailored\*.txt, data\tailored\*.md -Force -ErrorAction SilentlyContinue`) before stopping.
     - Then **STOP and ask the user for confirmation across all queued resumes**:
       > *"Have you used any of the skills in the **Candidate Skill Check** list(s) above that you'd like me to include for any of these roles, or should I generate `EngelhardtJoshuaResume.docx` and `EngelhardtJoshuaCV.docx` in `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/` for all queued positions using only your existing resume skills?"*
     - **Only** proceed to Step 4 to write the `.docx` files **after** the user replies and approves the changes!

4. **Save `EngelhardtJoshuaResume.docx` and `EngelhardtJoshuaCV.docx` Grouped by Business & Requisition Number in `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/` (After User Approval Only):**
   - **Only after the user has reviewed the preview and confirmed** (whether for 1 role or all roles in the queue), create the business and position subfolder with the requisition number inside `data/tailored/` (using `get_position_folder(company, title, external_id=..., url=...)` from `src.exporters.resume_tailor`, i.e., `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/`).
   - Load the user's active `.docx` resume in `data/` using `python-docx` (`from docx import Document`), apply the approved Professional Summary, Technical Skills adjustments, and bullet rewrites while preserving paragraph/run styling, and save the tailored resume with the exact filename **`EngelhardtJoshuaResume.docx`**:
     `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/EngelhardtJoshuaResume.docx`
   - Also generate the matching cover letter file using the same letterhead (`JOSH ENGELHARDT` + contact line from `data/EngelhardtJoshuaResume.docx`) and save it inside the exact same business/position folder with the filename **`EngelhardtJoshuaCV.docx`**:
     `data/tailored/<Company_Slug>/<Title_Slug>_<Req_ID>/EngelhardtJoshuaCV.docx`
   - Immediately **delete any temporary `.txt` or `.md` prompt files** in `data/tailored/` (`Remove-Item data\tailored\*.txt, data\tailored\*.md -Force -ErrorAction SilentlyContinue`) so `data/tailored/` only contains clean business folders (`<Company_Slug>/<Title_Slug>_<Req_ID>/`) holding `EngelhardtJoshuaResume.docx` and `EngelhardtJoshuaCV.docx`!
