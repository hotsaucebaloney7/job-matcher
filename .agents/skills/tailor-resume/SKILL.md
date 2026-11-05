---
name: tailor-resume
description: >-
  Automatically fetches jobs marked 'Want to Apply' from the user's Notion board,
  checks data/tailored/ to skip postings that already have a tailored resume file,
  loads the generated Argon prompt packets for new postings and the user's master
  .docx resume from data/, and generates tailored resume summaries, bullet rewrites,
  ATS keyword alignments, and tailored .docx resume files. Activate this skill whenever
  the user asks to tailor their resume, prepare an application, or process their Notion
  'Want to Apply' queue.
---

# Automated Notion 'Want to Apply' Resume Tailoring Skill

Use this skill whenever the user asks to tailor their resume for jobs in their Notion **`Want to Apply`** queue (or for a specific company/posting). You do **not** need the user to copy-paste the prompt manually—follow the automated steps below.

## Step-by-Step Procedure

1. **Refresh the Notion `'Want to Apply'` Queue & Check for Existing Tailored Resumes:**
   - First, inspect `data/tailored/` to see which companies/roles **already have a tailored resume file** (e.g., `Josh_Engelhardt_Resume_<Company>_<Title>.docx`).
   - Run the queue processor:
     ```powershell
     C:\Users\jenge\anaconda3\envs\job-matcher\python.exe -m src.exporters.resume_tailor
     ```
     Note: `process_want_to_apply_queue()` automatically checks `find_existing_tailored_resume(company, title)` in `data/tailored/` and **skips** any `'Want to Apply'` card that already has a tailored `.docx` resume file unless the user explicitly asks to tailor/regenerate that specific posting.
   - If the user explicitly names a posting that was already skipped (or asks to redo it), you can read the job's details directly from MySQL or run `process_want_to_apply_queue(..., force_regenerate=True)`.

2. **Never Recreate an Existing Tailored Resume in Batch Mode Unless Explicitly Asked:**
   - In general batch mode (*"tailor my resume"*), if a tailored `.docx` file **already exists** in `data/tailored/` for a `Company` + `Title`, **skip recreating it** and report:
     `[SKIP] <Company> — <Title> (already tailored: data/tailored/<filename>)`
   - Only process the **new / untailored** postings from the `'Want to Apply'` queue (or the specific posting the user asked for).

3. **Preview Changes First When a Specific Posting Is Requested (Human-in-the-Loop Approval):**
   - **CRITICAL RULE:** Whenever the user specifies a particular job posting to tailor (e.g., *"Tailor my resume for Reddit Ads Integrity"* or *"Tailor my resume for Blue Cross NC"*), **DO NOT write the `.docx` file immediately!**
   - Instead, present a clear **Before $\rightarrow$ After Proposed Changes Preview** in chat first:
     1. **Tailored Professional Summary:** Show **Current Summary** vs. **Proposed Tailored Summary (3–4 lines)** mirroring the role title, domain focus, and top matched/missing ATS keywords while remaining 100% truthful to the candidate's background.
     2. **Technical Skills Adjustments:** Show the exact **Current Line(s)** vs. **Proposed Line(s)** highlighting which missing/target ATS keywords are being woven in truthfully.
     3. **Experience & Project Bullet Rewrites:** For each bullet you plan to modify, show **Original Bullet** $\rightarrow$ **Proposed Rewritten Bullet** (`Action Verb + Technical Method/Tool + Quantified Impact`).
     4. **3-Sentence Cover Letter / Cold Outreach Hook:** Show a concise opening paragraph ready to paste into the application portal.
   - Then **STOP and ask the user for confirmation**:
     > *"Would you like me to generate `data/tailored/Josh_Engelhardt_Resume_<Company>_<Title>.docx` with these exact changes, or would you like to adjust any bullets first?"*
   - Only write the `.docx` file in Step 4 **after** the user approves (or if the user explicitly says *"generate the docx right away without previewing"*).

4. **Save a Consistently Named `.docx` File in `data/tailored/` & Clean Up Temporary `.txt`/`.md` Files:**
   - Once approved (or in batch mode), load the user's active `.docx` resume in `data/` using `python-docx` (`from docx import Document`), apply the approved Professional Summary, Technical Skills additions, and bullet adjustments while preserving paragraph/run styling, and save the file to:
     `data/tailored/Josh_Engelhardt_Resume_<Company_Slug>_<Title_Slug>.docx`
   - Immediately **delete any temporary `.txt` or `.md` prompt files** in `data/tailored/` (`Remove-Item data\tailored\*.txt, data\tailored\*.md -Force -ErrorAction SilentlyContinue`) so `data/tailored/` only contains clean, ready-to-upload `.docx` resumes!
