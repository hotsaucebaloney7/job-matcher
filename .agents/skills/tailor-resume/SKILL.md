---
name: tailor-resume
description: >-
  Automatically fetches jobs marked 'Want to Apply' from the user's Notion board,
  checks data/tailored/ position folders to skip postings that already have an
  EngelhardtJoshuaResume.docx file, loads the generated Argon prompt packets for
  new postings and the user's master .docx resume from data/, and generates tailored
  resume summaries, bullet rewrites, ATS keyword alignments, and tailored
  EngelhardtJoshuaResume.docx files inside position-named subfolders. Activate this
  skill whenever the user asks to tailor their resume, prepare an application, or
  process their Notion 'Want to Apply' queue.
---

# Automated Notion 'Want to Apply' Resume Tailoring Skill

Use this skill whenever the user asks to tailor their resume for jobs in their Notion **`Want to Apply`** queue (or for a specific company/posting). You do **not** need the user to copy-paste the prompt manually—follow the automated steps below.

## Step-by-Step Procedure

1. **Refresh the Notion `'Want to Apply'` Queue & Check for Existing Position Folders:**
   - First, inspect `data/tailored/` to see which position subfolders **already contain `EngelhardtJoshuaResume.docx`** (i.e., `data/tailored/<Company_Slug>_<Title_Slug>/EngelhardtJoshuaResume.docx`).
   - Run the queue processor:
     ```powershell
     C:\Users\jenge\anaconda3\envs\job-matcher\python.exe -m src.exporters.resume_tailor
     ```
     Note: `process_want_to_apply_queue()` automatically checks `find_existing_tailored_resume(company, title)` across `data/tailored/` subfolders and **skips** any `'Want to Apply'` card that already has `EngelhardtJoshuaResume.docx` inside its position folder unless the user explicitly asks to tailor/regenerate that specific posting.
   - If the user explicitly names a posting that was already skipped (or asks to redo it), you can read the job's details directly from MySQL or run `process_want_to_apply_queue(..., force_regenerate=True)`.

2. **Never Recreate an Existing Tailored Resume in Batch Mode Unless Explicitly Asked:**
   - In general batch mode (*"tailor my resume"*), if `data/tailored/<Position_Folder>/EngelhardtJoshuaResume.docx` **already exists** for a `Company` + `Title`, **skip recreating it** and report:
     `[SKIP] <Company> — <Title> (already tailored: data/tailored/<Position_Folder>/EngelhardtJoshuaResume.docx)`
   - Only process the **new / untailored** postings from the `'Want to Apply'` queue (or the specific posting the user asked for).

3. **Preview Changes First & Enforce Strict Closed-World Skills (Human-in-the-Loop Approval):**
   - **STRICT CLOSED-WORLD SKILL RULE (NO UNAPPROVED SKILLS IN RESUME):**
     - You must **ONLY** use skills, tools, frameworks, and methodologies that **already exist on the user's Master Resume** when drafting the Tailored Summary, Technical Skills, and Experience/Project Bullets.
     - **NEVER** sneak new skills, tools, or methods from the job description (or from missing ATS skill groups) into the resume draft itself unless the user has explicitly confirmed they have done them.
     - Instead, collect all missing/suggested skills from the job posting into **one single, dedicated section** in your response (`### ❓ Candidate Skill Check — Have You Done Any of These?`) so the user can review them in one place and tell you which ones (if any) they have actually done.
   - **CRITICAL PREVIEW RULE:** Whenever the user specifies a particular job posting to tailor (or in batch mode before writing `.docx` files if new skills are in question), **DO NOT write the `.docx` file immediately!**
   - Present a clear **Before $\rightarrow$ After Proposed Changes Preview** in chat first:
     1. **Tailored Professional Summary:** Show **Current Summary** vs. **Proposed Tailored Summary (3–4 lines)** using **only** skills and experience already present on the master resume, framed to match the role's focus.
     2. **Technical Skills Adjustments (Reordering / Emphasis Only):** Show **Current Line(s)** vs. **Proposed Line(s)** reordering or emphasizing existing master-resume skills to match the posting—with **zero unverified new skills added**.
     3. **Experience & Project Bullet Rewrites:** For each bullet you plan to modify, show **Original Bullet** $\rightarrow$ **Proposed Rewritten Bullet** (`Action Verb + Technical Method/Tool + Quantified Impact`) using **only** tools and work already on the master resume.
     4. **❓ Candidate Skill Check — Have You Done Any of These? (Single Consolidated List):** List all high-value skills/tools from the job description that are **not** currently on the master resume in one single bulleted checklist, asking the user if they have experience with any of them and want them added.
     5. **3-Sentence Cover Letter / Cold Outreach Hook:** Show a concise opening paragraph ready to paste into the application portal.
   - Then **STOP and ask the user for confirmation**:
     > *"Have you used any of the skills in the **Candidate Skill Check** list above that you'd like me to include, or should I generate `data/tailored/<Position_Folder>/EngelhardtJoshuaResume.docx` using only your existing resume skills?"*
   - Only write the `.docx` file in Step 4 **after** the user approves.

4. **Save `EngelhardtJoshuaResume.docx` Inside a Position-Named Folder in `data/tailored/` & Clean Up Temporary `.txt`/`.md` Files:**
   - Once approved (or in batch mode), create a subfolder inside `data/tailored/` named after the company and position (using `get_position_folder(company, title)` from `src.exporters.resume_tailor`, i.e., `data/tailored/<Company_Slug>_<Title_Slug>/`).
   - Load the user's active `.docx` resume in `data/` using `python-docx` (`from docx import Document`), apply the approved Professional Summary, Technical Skills adjustments, and bullet rewrites while preserving paragraph/run styling, and save the file with the exact filename **`EngelhardtJoshuaResume.docx`**:
     `data/tailored/<Company_Slug>_<Title_Slug>/EngelhardtJoshuaResume.docx`
   - Immediately **delete any temporary `.txt` or `.md` prompt files** in `data/tailored/` (`Remove-Item data\tailored\*.txt, data\tailored\*.md -Force -ErrorAction SilentlyContinue`) so `data/tailored/` only contains clean position folders holding `EngelhardtJoshuaResume.docx`!
