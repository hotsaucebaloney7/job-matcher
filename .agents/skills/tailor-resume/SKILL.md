---
name: tailor-resume
description: >-
  Automatically fetches jobs marked 'Want to Apply' from the user's Notion board,
  loads the generated Argon prompt packets from data/tailored/ and the user's master
  .docx resume from data/, and generates tailored resume summaries, bullet rewrites,
  ATS keyword alignments, and tailored .docx resume files. Activate this skill whenever
  the user asks to tailor their resume, prepare an application, or process their Notion
  'Want to Apply' queue.
---

# Automated Notion 'Want to Apply' Resume Tailoring Skill

Use this skill whenever the user asks to tailor their resume for jobs in their Notion **`Want to Apply`** queue (or for a specific company). You do **not** need the user to copy-paste the prompt manually—follow the automated steps below.

## Step-by-Step Procedure

1. **Refresh the Notion `'Want to Apply'` Queue Packet:**
   Run the queue processor so `data/tailored/` has the latest `.txt` prompt files from Notion and MySQL:
   ```powershell
   C:\Users\jenge\anaconda3\envs\job-matcher\python.exe -m src.exporters.resume_tailor
   ```

2. **Read the Target Prompt File(s) in `data/tailored/`:**
   - If the user specified a company (e.g., *"Tailor my resume for Blue Cross NC"*), read the matching `data/tailored/YYYY-MM-DD_<Company>_<Title>_Argon_Prompt.txt` file.
   - If the user asked to process their queue generally (e.g., *"Tailor my resume"*), check `data/tailored/argon_tailoring_prompt_YYYY-MM-DD.txt` and either tailor for all jobs in the queue or ask which one they want to generate a `.docx` for first.

3. **Generate the 4 Tailored Deliverables for Each Target Role:**
   - **1. Tailored Professional Summary (3–4 lines):** Mirror the exact role title, domain focus, and top matched/missing ATS keywords while remaining 100% truthful to the candidate's background (M.S. in Data Science & Engineering, B.S. in Cybersecurity, IRS-CI Data Scientist GS-9, U.S. Army Information Systems Specialist, ML/Data Engineering portfolio).
   - **2. High-Impact Experience & Project Bullet Rewrites (4–6 bullets):** Rewrite the most relevant master resume bullets using `Action Verb + Technical Method/Tool + Quantified Impact` to align with the posting's core responsibilities.
   - **3. ATS Skills & Keywords Alignment:** List which **Missing Skill Groups** from the prompt packet can be truthfully highlighted in the Technical Skills section or project descriptions.
   - **4. 3-Sentence Cover Letter / Cold Outreach Hook:** A concise opening paragraph ready to paste into the application portal.

4. **Generate a Ready-to-Upload Tailored `.docx` File (When Requested or Helpful):**
   - Load the user's active `.docx` resume in `data/` using `python-docx` (`from docx import Document`), apply the tailored Professional Summary, Technical Skills additions, and top bullet adjustments while preserving paragraph/run styling, and save the new file to:
     `data/tailored/Josh_Engelhardt_Resume_<Company>.docx`
