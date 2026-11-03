# Job Matcher — Automated Multi-Source Job Ingestion, Hybrid NLP/Embedding Scoring & Notion CRM Pipeline

[![Python 3.11](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MySQL 8.0](https://img.shields.io/badge/MySQL-8.0-4479A1?logo=mysql&logoColor=white)](https://www.mysql.com/)
[![SQLAlchemy 2.0](https://img.shields.io/badge/SQLAlchemy-2.0_ORM-D71F00?logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org/)
[![Embeddings](https://img.shields.io/badge/Embeddings-BAAI%2Fbge--base--en--v1.5-FF9D00?logo=huggingface&logoColor=white)](https://huggingface.co/BAAI/bge-base-en-v1.5)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Notion API](https://img.shields.io/badge/Notion_API-2022--06--28-000000?logo=notion&logoColor=white)](https://developers.notion.com/)
[![Pytest](https://img.shields.io/badge/Pytest-86_Passed-0A9EDC?logo=pytest&logoColor=white)](tests/)

---

## 1. Executive Overview

Modern technical job searches suffer from severe signal-to-noise degradation: job seekers must manually check dozens of fragmented Applicant Tracking Systems (ATS) and job aggregators, filter out senior/principal roles requiring `6+` years of experience, verify geographic or US-Remote eligibility, and evaluate technical stack alignment across thousands of unstructured HTML descriptions.

**Job Matcher** is a production-grade, 4-stage ETL, NLP, and CRM synchronization pipeline engineered to automate job discovery, semantic fit ranking, and application lifecycle tracking:

- **Stage 1 — Multi-Source REST API Ingestion & Knockout Filtering:** Polls **6,200+ raw job postings per run** across **8 live APIs**—direct ATS boards (**Greenhouse**, **Lever**, **Ashby**) across 40+ AI/tech companies plus external aggregators (**JSearch / Google for Jobs**, **Adzuna**, **The Muse**, **Remotive**, and the federal **USAJOBS** REST API with OPM STEM series codes). Applies deterministic regex knockout filters for target role families, US-Remote or local Raleigh/RTP ($\le$ 25-minute commute radius) eligibility, and required years of experience (`1–3 yrs` primary target, `3–5 yrs` stretch tier, `6+ yrs` hard knockout).
- **Stage 2 — Idempotent Relational Storage & Lifecycle Tracking:** Normalizes and persists qualifying postings into a Dockerized **MySQL 8.0** database via **SQLAlchemy 2.0 ORM**. Uses deterministic SHA-256 (`job_hash`) deduplication and `ON DUPLICATE KEY UPDATE` upserts to track `date_fetched` and `date_last_seen` while automatically expiring offline postings (`expire_stale_postings`).
- **Stage 3 — Hybrid NLP Keyword Rubric + Local Transformer Embedding Scorer:** Parses the candidate's active `.pdf`, `.docx`, `.txt`, or `.md` resume and scores every unscored job posting by combining a weighted **10-category technical keyword rubric** (`config/keywords.yaml`) with **768-dimensional L2-normalized semantic embeddings** (`BAAI/bge-base-en-v1.5` running locally on CPU via `sentence-transformers`).
- **Stage 4 — Automated Markdown Digest & State-Aware Notion CRM Sync:** Renders a Rich CLI leaderboard, writes a daily Markdown report (`reports/daily_matches_YYYY-MM-DD.md`) with matched vs. missing skill-gap breakdowns, and synchronizes qualifying matches (`match_score >= 65.0`) to a 4-view **Notion CRM database** while automatically archiving taken-down unapplied listings.

---

## 2. System Architecture Diagram

```mermaid
graph TD
    subgraph Stage1["Stage 1: Multi-Source API Ingestion & Knockout Filtering"]
        A1["Direct ATS APIs<br/>(Greenhouse, Lever, Ashby)"]
        A2["Aggregator & Gov APIs<br/>(JSearch, Adzuna, The Muse, Remotive, USAJOBS)"]
        F1["HTML Normalization & Salary Parser<br/>(BeautifulSoup4 + Regex)"]
        F2["Deterministic Knockout Engine<br/>(Role Family + RTP/Remote + <6 Yrs Exp)"]
        A1 --> F1
        A2 --> F1
        F1 --> F2
    end

    subgraph Stage2["Stage 2: Dockerized MySQL 8.0 Storage & Lifecycle"]
        DB[("MySQL 8.0 Database<br/>(SQLAlchemy 2.0 ORM)")]
        U1["SHA-256 job_hash Deduplication<br/>(ON DUPLICATE KEY UPDATE)"]
        E1["Stale Listing Expiration<br/>(expire_stale_postings + Health Guards)"]
        F2 --> U1
        U1 --> DB
        DB --> E1
    end

    subgraph Stage3["Stage 3: Hybrid NLP & Transformer Scoring Engine"]
        R1["Dynamic Resume Loader<br/>(.pdf / .docx / .txt + SHA-256 Change Detection)"]
        K1["10-Category Keyword Rubric<br/>(Word-Boundary Regex + Snippet Normalization)"]
        B1["Local Transformer Encoder<br/>(BAAI/bge-base-en-v1.5 768-d Cosine Similarity)"]
        C1["Composite Score & Stretch Calibration<br/>(0.50 * Keyword + 0.50 * Semantic + Exp Adj)"]
        DB --> K1
        DB --> B1
        R1 --> B1
        K1 --> C1
        B1 --> C1
        C1 --> DB
    end

    subgraph Stage4["Stage 4: Reporting & Two-Way Notion CRM Sync"]
        M1["Rich CLI Leaderboard &<br/>Daily Markdown Digest (reports/)"]
        N1["Notion REST API Sync<br/>(Auto-Schema + 4 Kanban/Table Views)"]
        N2["State-Aware Notion Cleanup<br/>(Archive Unapplied / Preserve Applied)"]
        DB --> M1
        DB --> N1
        E1 --> N2
    end
```

---

## 3. Deep-Dive Engineering Highlights

### 3.1 Idempotent `ON DUPLICATE KEY UPDATE` Upserts & Source-Health Expiration Safeguards
Cross-aggregator job ingestion frequently encounters the same role reposted across multiple boards or refreshed with updated URLs.
- **Deterministic Hashing (`compute_job_hash`):** Generates a canonical SHA-256 digest from `sha256(f"{company.lower().strip()}|{title.lower().strip()}|{workplace_type.lower().strip()}")`.
- **Non-Destructive Upserts (`upsert_job_postings`):** Executes bulk MySQL `INSERT ... ON DUPLICATE KEY UPDATE` statements that refresh `date_last_seen = NOW()` and backfill missing salary bounds via `COALESCE(inserted.salary_min, job_postings.salary_min)` without overwriting existing `match_score`, `status`, or `notion_page_id` columns.
- **Fault-Tolerant Expiration (`expire_stale_postings`):** Distinguishes between **full-board sources** (`greenhouse`, `lever`, `ashby`, `remotive`) and **paginated search APIs** (`jsearch`, `adzuna`, `themuse`, `usajobs`). Unapplied (`status = 'new'`) rows from full-board sources are marked closed immediately if absent from a healthy scrape—while any company board that experienced an HTTP timeout or non-200 response during Stage 1 is added to `failed_companies` and exempted from expiration so transient network failures never cause false positives. Paginated search sources apply a configurable 3-day grace window (`date_last_seen < NOW() - INTERVAL 3 DAY`).

### 3.2 Hybrid NLP Scoring Formula, Snippet Normalization & Experience Calibration
Pure keyword counters fail on synonyms and context, whereas pure dense embeddings can over-score generic boilerplate descriptions that lack mandatory core tools (e.g., SQL, PyTorch, or AWS). Stage 3 combines both signals:

$$\text{Composite Score} = \text{clip}_{[0, 100]}\!\left(0.50 \times S_{\text{keyword}} + 0.50 \times S_{\text{semantic}} + \Delta_{\text{snippet}} + \Delta_{\text{exp}}\right)$$

- **Weighted Keyword Rubric ($S_{\text{keyword}} \in [0, 100]$):** Evaluates 10 weighted technical categories defined in [`config/keywords.yaml`](config/keywords.yaml) (`Core ML / AI`, `Applied AI / LLMs`, `Python Ecosystem`, `SQL & Databases`, `ML Frameworks`, `Cloud Infrastructure`, `Data Engineering`, `Backend / APIs`, `Containers & CI/CD`, and `Experimentation`) using compiled word-boundary regular expressions (`\b...\b`).
- **768-d Transformer Semantic Similarity ($S_{\text{semantic}} \in [0, 100]$):** Encodes the active resume and each job description (`f"{title}\n{description_text}"`) into L2-normalized 768-dimensional vectors using `BAAI/bge-base-en-v1.5`, scaling raw cosine similarity $(\cos \theta)$ into a calibrated `0–100` distribution.
- **Snippet-Length Normalization ($\Delta_{\text{snippet}}$):** Aggregators like Adzuna return 500-character snippets rather than full 4,000-character descriptions. When `len(description_text) < 600` (`is_snippet=True`), the scorer shifts weight toward dense semantic similarity (`0.35 * Keyword + 0.65 * Semantic`) and applies a `+8.0` truncation compensation offset so concise snippets compete fairly with full ATS descriptions.
- **Experience-Tier Calibration ($\Delta_{\text{exp}}$):** [`extract_min_years_experience`](src/scoring/filters.py) parses minimum required years across patterns like `3+ years`, `2-4 yrs`, and `at least 5 years`:
  - **Primary Target (`1–3 yrs` or Unspecified Non-Senior):** $\Delta_{\text{exp}} = 0.0$
  - **Stretch Tier (`4–5 yrs` or `Senior` title with unspecified years):** $\Delta_{\text{exp}} = -3.0$
  - **Hard Knockout (`6+ yrs` minimum required):** Rejected at Stage 1 and marked `status = 'rejected'` at Stage 3.

### 3.3 Dynamic `.pdf` / `.docx` Resume Discovery & SHA-256 Change Detection
Users can drop any `.pdf`, `.docx`, `.txt`, or `.md` resume into [`data/`](data/).
- [`discover_resume_file`](src/scoring/embed_scorer.py) prioritizes `.pdf` and `.docx` files over fallback `.txt` files, extracting text via `pypdf` and `python-docx`.
- On every run, [`run_job_matcher.ps1`](run_job_matcher.ps1) computes the SHA-256 file hash of the active resume and compares it against `data/.last_resume_hash`. Whenever the resume file is modified or replaced, the runner automatically injects `--rescore` to recompute all database embeddings and composite scores against the updated resume.

### 3.4 State-Aware Two-Way Notion CRM Synchronization
[`src/exporters/notion_sync.py`](src/exporters/notion_sync.py) connects directly to the Notion REST API (`2022-06-28`):
- **Automatic Schema Provisioning & Child-Database Resolution:** Accepts either a raw 32-character Notion database ID or a full parent page URL, resolves inline `child_database` blocks automatically, and provisions all 14 tracker columns (`Company`, `Match Score`, `Keyword Score`, `Semantic Score`, `Experience Tier`, `Workplace`, `Status`, `Date Applied`, `Follow-Up Date`, `Contact / Notes`, `Salary`, `Source`, `Location`, `URL`).
- **State-Aware Expiration Cleanup (`archive_expired_notion_pages`):** Before archiving an expired job's Notion page, the exporter queries `GET /v1/pages/{page_id}` to inspect the user's current `Status` property on the Notion board:
  - Cards still sitting in **`New Match`** or **`Want to Apply`** are archived (`PATCH {"archived": true}`) so the user's **`To Apply Queue`** only contains live openings.
  - Cards moved by the user to **`Applied`**, **`Follow Up Needed`**, **`Interviewing`**, **`Offer`**, or **`Declined / Rejected`** are **preserved on the Notion board** (and updated to `status = 'applied'` in MySQL) so application history is never lost after a company closes the listing.

---

## 4. Repository Directory Tree

```text
job-matcher/
├── config/
│   ├── companies.yaml                       # 40+ curated Greenhouse, Lever & Ashby target company slugs
│   └── keywords.yaml                        # 10-category weighted technical skill taxonomy (sums to 100 pts)
├── data/
│   └── resume.txt                           # Drop-in folder for active resume (.pdf, .docx, .txt, or .md)
├── reports/
│   └── daily_matches_YYYY-MM-DD.md          # Generated daily Markdown digests with skill-gap breakdowns
├── src/
│   ├── __init__.py
│   ├── main.py                              # End-to-end 4-stage CLI orchestrator (--min-score, --rescore, --skip-ingest)
│   ├── db/
│   │   ├── __init__.py
│   │   ├── models.py                        # SQLAlchemy 2.0 JobPosting ORM model (MySQL 8.0 & SQLite compatible)
│   │   └── session.py                       # Engine init, Docker auto-start, SHA-256 upserts & expire_stale_postings
│   ├── exporters/
│   │   ├── __init__.py
│   │   ├── markdown_digest.py               # Rich CLI leaderboard & Markdown report generator
│   │   └── notion_sync.py                   # Notion API auto-schema provisioning, card sync & state-aware archiving
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── adzuna_client.py                 # Adzuna API client (Raleigh 24km radius + US-Remote)
│   │   ├── ats_poller.py                    # Greenhouse, Lever & Ashby public ATS board pollers
│   │   └── external_apis.py                 # Remotive, The Muse, JSearch (RapidAPI v2) & USAJOBS REST clients
│   └── scoring/
│       ├── __init__.py
│       ├── embed_scorer.py                  # Local BAAI/bge-base-en-v1.5 scorer & multi-format resume parser
│       ├── filters.py                       # Role, commute/Remote & <6 yr experience knockout filters
│       └── keyword_scorer.py                # Word-boundary regex rubric scorer & skill-gap analyzer
├── tests/
│   ├── __init__.py
│   ├── test_step1_filters_and_ingest.py     # 60 unit tests: title, location, experience & ATS parsers
│   ├── test_step2_db.py                     # 3 integration tests: schema creation, upserts & deduplication
│   ├── test_step3_scoring.py                # 15 unit tests: keyword rubric, BGE embeddings & stretch penalties
│   └── test_step4_pipeline_and_exporters.py # 8 integration tests: aggregators, digest, Notion sync & expiration
├── .env.example                             # Template environment variables for MySQL, APIs & Notion sync
├── .gitignore
├── docker-compose.yml                       # MySQL 8.0 service with UTF8MB4 & persistent volume healthchecks
├── job_matcher_specification.md             # Full architectural specification document
├── pytest.ini                               # Pytest configuration
├── requirements.txt                         # Python 3.11 dependencies
└── run_job_matcher.ps1                      # One-click PowerShell automation runner
```

---

## 5. Quickstart & Installation Guide

### Prerequisites
- **Conda / Miniconda** (Python `3.11`)
- **Docker Desktop** (for the containerized MySQL 8.0 database)

### 1. Create the Conda Environment & Install Dependencies
```powershell
conda create -n job-matcher python=3.11 -y
conda activate job-matcher
pip install -r requirements.txt
```

### 2. Configure `.env` Credentials
Copy `.env.example` to `.env` and populate your API keys (Greenhouse, Lever, Ashby, and Remotive work with zero keys out of the box):
```powershell
Copy-Item .env.example .env
```
```ini
DATABASE_URL=mysql+pymysql://jobmatcher:jobmatcher_pass@localhost:3306/job_matcher?charset=utf8mb4
ADZUNA_APP_ID=your_adzuna_app_id
ADZUNA_API_KEY=your_adzuna_api_key
JSEARCH_RAPIDAPI_KEY=your_rapidapi_jsearch_key
THE_MUSE_API_KEY=your_themuse_api_key
USAJOBS_API_KEY=your_usajobs_api_key
USAJOBS_USER_EMAIL=your_email@example.com
NOTION_API_KEY=ntn_your_notion_integration_secret
NOTION_DATABASE_ID=https://www.notion.so/your_database_or_page_link
```

### 3. Start the MySQL 8.0 Container
```powershell
docker compose up -d --wait
```

### 4. Add Your Resume & Run the One-Click Pipeline
Drop your resume (`.pdf`, `.docx`, `.txt`, or `.md`) into the `data/` folder, then execute the PowerShell runner:
```powershell
# Standard daily run (auto-starts Docker/MySQL, detects resume edits, polls 8 APIs, scores & syncs to Notion)
.\run_job_matcher.ps1

# Force re-scoring of all stored postings with a custom threshold
.\run_job_matcher.ps1 -Rescore -MinScore 60

# Run the full 86-test Pytest QA suite before executing the pipeline
.\run_job_matcher.ps1 -RunTests
```

### 5. Run the Automated QA Test Suite Directly
```powershell
pytest -v
```
