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

Use this skill whenever the user asks to tailor their resume for jobs in their Notion **`Want to Apply`** queue (or for a specific company). You do **not** need the user to copy-paste the prompt manually—follow the automated steps below.

## Step-by-Step Procedure

1. **Refresh the Notion `'Want to Apply'` Queue & Check for Existing Tailored Resumes:**
   - First, inspect `data/tailored/` to see which companies/roles **already have a tailored resume file** (e.g., `Josh_Engelhardt_Resume_<Company>_<Title>.docx` or `*_Tailored_Resume.*`).
   - Run the queue processor:
     ```powershell
     C:\Users\jenge\anaconda3\envs\job-matcher\python.exe -m src.exporters.resume_tailor
     ```
     Note: `process_want_to_apply_queue()` automatically checks `find_existing_tailored_resume(company, title)` in `data/tailored/` and **skips** any `'Want to Apply'` card that already has a tailored resume file unless the user explicitly asks to regenerate it!

2. **Never Recreate an Existing Tailored Resume Unless Explicitly Asked:**
   - Before generating a tailored resume for any posting, check if a matching `.docx`, `.pdf`, or `_Tailored_Resume` file already exists in `data/tailored/` for that `Company` + `Title`.
   - If a tailored resume file **already exists** in `data/tailored/`, **skip recreating it** and simply report to the user:
     `⏭️ Skipped <Company> — <Title> (already tailored: data/tailored/<filename>)`
   - Only process the **new / untailored** postings from the `'Want to Apply'` queue!

3. **Generate the 4 Tailored Deliverables for Each New Target Role:**
   - **1. Tailored Professional Summary (3–4 lines):** Mirror the exact role title, domain focus, and top matched/missing ATS keywords while remaining 100% truthful to the candidate's background (M.S. in Data Science & Engineering, B.S. in Cybersecurity, IRS-CI Data Scientist GS-9, U.S. Army Information Systems Specialist, ML/Data Engineering portfolio).
   - **2. High-Impact Experience & Project Bullet Rewrites (4–6 bullets):** Rewrite the most relevant master resume bullets using `Action Verb + Technical Method/Tool + Quantified Impact` to align with the posting's core responsibilities.
   - **3. ATS Skills & Keywords Alignment:** List which **Missing Skill Groups** from the prompt packet can be truthfully highlighted in the Technical Skills section or project descriptions.
   - **4. 3-Sentence Cover Letter / Cold Outreach Hook:** A concise opening paragraph ready to paste into the application portal.

4. **Always Save a Consistently Named `.docx` File in `data/tailored/` & Clean Up Temporary `.txt`/`.md` Files:**
   - Load the user's active `.docx` resume in `data/` using `python-docx` (`from docx import Document`), apply the tailored Professional Summary, Technical Skills additions, and top bullet adjustments while preserving paragraph/run styling, and save the file using this exact naming convention so future runs automatically detect it and skip it:
     `data/tailored/Josh_Engelhardt_Resume_<Company_Slug>_<Title_Slug>.docx`
   - Once the tailored `.docx` resume file(s) are saved in `data/tailored/`, **delete any `.txt` or `.md` prompt files** in `data/tailored/` (`Remove-Item data\tailored\*.txt, data\tailored\*.md -Force`) so the `data/tailored/` folder only contains clean, ready-to-upload `.docx` resumes!
