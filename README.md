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

- **Stage 1 — Multi-Source REST API Ingestion & Knockout Filtering:** Polls **6,200+ raw job postings per run** across **8 live APIs**—direct ATS boards (**Greenhouse**, **Lever**, **Ashby**) across 40+ AI/tech companies plus external aggregators (**JSearch / Google for Jobs**, **Adzuna**, **The Muse**, **Remotive**, and the federal **USAJOBS** REST API with OPM STEM series codes). Applies deterministic regex knockout filters for target role families, US-Remote or local Raleigh/RTP ($\le 25$-minute commute radius) eligibility, and required years of experience (`1–3 yrs` primary target, `3–5 yrs` stretch tier, `6+ yrs` hard knockout).
- **Stage 2 — Idempotent Relational Storage & Lifecycle Tracking:** Normalizes and persists qualifying postings into a Dockerized **MySQL 8.0** database via **SQLAlchemy 2.0 ORM**. Uses deterministic SHA-256 (`job_hash`) deduplication and `ON DUPLICATE KEY UPDATE` upserts to track `date_fetched` and `date_last_seen` while automatically expiring offline postings (`expire_stale_postings`).
- **Stage 3 — Hybrid NLP Keyword Rubric + Local Transformer Embedding Scorer:** Parses the candidate's active `.pdf`, `.docx`, `.txt`, or `.md` resume and scores every unscored job posting by combining a weighted **10-category technical keyword rubric** (`config/keywords.yaml`) with **768-dimensional L2-normalized semantic embeddings** (`BAAI/bge-base-en-v1.5` running locally on CPU via `sentence-transformers`).
- **Stage 4 — Automated Markdown Digest & State-Aware Notion CRM Sync:** Renders a Rich CLI leaderboard, writes a daily Markdown report (`reports/daily_matches_YYYY-MM-DD.md`) with matched vs. missing skill-gap breakdowns, and synchronizes qualifying matches (`match_score >= 65.0`) to a 4-view **Notion CRM database** while automatically archiving taken-down unapplied listings.

---

## 2. System Architecture & Pipeline Benchmarks

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

### Operational Telemetry & Performance Benchmarks
| Pipeline Stage | Throughput / Volume | Typical Latency | Key Mechanism |
| :--- | :--- | :--- | :--- |
| **Stage 1: Multi-API Ingestion** | `6,200+` raw postings scanned / run | `~18–25 sec` | HTTP session pooling across 40+ ATS boards & 5 aggregators |
| **Stage 1: Knockout Filtering** | `~96%` noise reduction | `< 0.4 sec` | Pre-compiled regex knockouts (Title, Geo/Remote, `<6 yrs` tenure) |
| **Stage 2: MySQL 8.0 Bulk Upsert** | `150–300` qualifying rows / run | `< 0.15 sec` | Batched `INSERT ... ON DUPLICATE KEY UPDATE` via SQLAlchemy 2.0 |
| **Stage 3: Hybrid BGE Scoring** | `~25–40` postings / sec (CPU) | `~3–6 sec` (incremental) | Local `BAAI/bge-base-en-v1.5` 768-d embeddings + 10-group regex |
| **Stage 4: Digest & Notion CRM** | `10–25` top matches (`score >= 65.0`) | `~1–4 sec` | Idempotent `notion_page_id` tracking & state-aware archival |

---

## 3. Sample Output & Live Production Telemetry

Below is an excerpt from a live production run (`reports/daily_matches_2026-11-03.md`), demonstrating how the pipeline balances **Keyword Rubric**, **768-d Semantic Similarity**, **Snippet Normalization** (Notice Rank #7 on Adzuna), and **Stretch Experience Calibration** (`-3.0` adjustment on `4–5 yr` roles):

### 3.1 Summary Leaderboard (`match_score >= 65.0`)

| Rank | Match Score | Keyword | Semantic | Exp. Tier | Mode | Company | Role | Salary |
| ---: | ----------: | ------: | -------: | :-------- | :--: | :------ | :--- | :----- |
| 1 | **80.6** | 90.0 | 71.3 | `3+ yrs (Primary 1-3)` | `[REMOTE]` | **GitLab** | [Data Scientist, Enterprise Analytics](https://job-boards.greenhouse.io/gitlab/jobs/8922013002) | Not listed |
| 2 | **80.3** | 90.0 | 70.6 | `Unspecified (Target)` | `[REMOTE]` | **Kyndryl** | [Data Engineer, Forward Deployed Engineer](https://www.themuse.com/jobs/kyndryl/data-engineer-forward-deployed-engineer) | Not listed |
| 3 | **76.1** | 95.0 | 63.3 | `4+ yrs (Stretch 3-5)` | `[LOCAL]` | **AECOM** | [Environmental Data Scientist](https://www.indeed.com/viewjob?jk=8fa7006777631b2f) | $169,982 |
| 4 | **74.2** | 80.0 | 68.3 | `1+ yrs (Primary 1-3)` | `[REMOTE]` | **Maximus** | [Data Scientist (Remote)](https://jobgether.com/offer/6ae2e83ed6c8b081a78f0cc9-data-scientist-remote) | Not listed |
| 6 | **72.6** | 80.0 | 71.2 | `5+ yrs (Stretch 3-5)` | `[REMOTE]` | **CrowdStrike** | [Engineer III - Platform Data Engineer (Remote)](https://www.themuse.com/jobs/crowdstrike/engineer-iii-platform-data-engineer-remote-744563) | Not listed |
| 7 | **71.6** | 50.0 | 70.9 | `Unspecified (Target)` | `[LOCAL]` | **Nityo Infotech** | [AI Engineer/ Architect](https://www.adzuna.com/land/ad/5945757210) | $116,063 |
| 9 | **71.1** | 80.0 | 68.2 | `5+ yrs (Stretch 3-5)` | `[REMOTE]` | **Ramp** | [Machine Learning Engineer](https://jobs.ashbyhq.com/ramp/2888b101-b1da-4e53-a02e-1bb9b1b5a951) | $200,000 - $330,000 |
| 12 | **66.1** | 60.0 | 72.2 | `3+ yrs (Primary 1-3)` | `[REMOTE]` | **OpenAI** | [Data Engineer, Core Experimentation](https://jobs.ashbyhq.com/openai/2796d32a-9f7c-4008-a2c5-50dd53b0f2fe) | $347,000 - $385,000 |

### 3.2 Detailed Skill-Gap Diagnostic Cards

> #### 1. `[REMOTE]` [GitLab — Data Scientist, Enterprise Analytics](https://job-boards.greenhouse.io/gitlab/jobs/8922013002)
> - **Composite Match Score:** **80.6 / 100** *(Keyword Rubric: `90.0`, Resume Embedding Similarity: `71.3`)*
> - **Experience Required:** `3+ yrs (Primary 1-3)` | **Location:** Remote, United States | **Salary Range:** Not listed | **Source:** `greenhouse`
> - **Matched Skill Tags:** `Core ML / AI` (machine learning, statistical modeling), `ML Frameworks` (scikit-learn, xgboost), `Experimentation` (a/b testing, causal inference), `SQL & Databases` (sql), `Python Ecosystem` (python, pandas, numpy), `Applied AI / LLMs` (agent), `Containers & CI/CD` (ci/cd), `Cloud Infrastructure` (cloud)
> - **Missing Skill Tags:** `Data Engineering`, `Backend / APIs`, `Graduate Degree`
>
> #### 3. `[LOCAL]` [AECOM — Environmental Data Scientist](https://www.indeed.com/viewjob?jk=8fa7006777631b2f)
> - **Composite Match Score:** **76.1 / 100** *(Keyword Rubric: `95.0`, Resume Embedding Similarity: `63.3`, Stretch Adj: `-3`)*
> - **Experience Required:** `4+ yrs (Stretch 3-5)` | **Location:** Raleigh, NC | **Salary Range:** $169,982 | **Source:** `jsearch`
> - **Matched Skill Tags:** `Core ML / AI` (machine learning), `ML Frameworks` (scikit-learn), `SQL & Databases` (sql), `Data Engineering` (etl), `Python Ecosystem` (python, pandas), `Applied AI / LLMs` (rag, generative ai), `Containers & CI/CD` (git), `Cloud Infrastructure` (cloud)
> - **Missing Skill Tags:** `Backend / APIs`, `Graduate Degree`, `Experimentation`

---

## 4. Deep-Dive Engineering Highlights

### 4.1 Relational Schema (`job_postings`), Idempotent Upserts & Expiration Safeguards
Cross-aggregator job ingestion frequently encounters the same role reposted across multiple boards or refreshed with updated URLs.

| Column | Type | Constraints / Indexing | Purpose |
| :--- | :--- | :--- | :--- |
| `job_hash` | `CHAR(64)` | `PRIMARY KEY` | Deterministic SHA-256 of normalized `company \| title \| workplace_type` |
| `source` | `VARCHAR(32)` | `NOT NULL` | Origin feed (`greenhouse`, `lever`, `ashby`, `jsearch`, `adzuna`, `themuse`, `remotive`, `usajobs`) |
| `company` / `title` | `VARCHAR` | `NOT NULL`, `INDEX` | Normalized employer name and role title |
| `workplace_type` | `ENUM` | `INDEX` (`Remote`, `Local`, `Hybrid`) | Validated against 25-min Raleigh/RTP commute radius or US-Remote rules |
| `salary_min` / `max` | `INT` | `NULL` | Annualized USD salary bounds extracted from structured fields or regex |
| `description_text` | `LONGTEXT` | `NOT NULL` | HTML-stripped plain text job description |
| `date_fetched` / `last_seen`| `DATETIME` | `NOT NULL` | Lifecycle timestamps updated via `ON DUPLICATE KEY UPDATE` |
| `keyword_score` / `semantic_score` | `FLOAT` | `NULL` | Component scores ($0\text{--}100$) from Stage 3 rubric and `bge-base-en-v1.5` |
| `match_score` | `FLOAT` | `INDEX`, `NULL` | Final composite score ($0\text{--}100$); `NULL` triggers Stage 3 evaluation |
| `matched_keywords` | `JSON` | `NULL` | Serialized dictionary of matched Tier 1–3 tags, missing categories, and exp tier |
| `status` | `ENUM` | `INDEX` (`new`, `reviewed`, `applied`, `rejected`) | Application workflow state preserved across re-ingestions |
| `notion_page_id` | `VARCHAR(64)` | `NULL` | Two-way Notion CRM foreign key for state-aware synchronization |

- **Non-Destructive Upserts (`upsert_job_postings`):** Executes bulk MySQL `INSERT ... ON DUPLICATE KEY UPDATE` statements that refresh `date_last_seen = NOW()` and backfill missing salary bounds via `COALESCE(inserted.salary_min, job_postings.salary_min)` without overwriting existing `match_score`, `status`, or `notion_page_id` columns.
- **Fault-Tolerant Expiration (`expire_stale_postings`):** Distinguishes between **full-board sources** (`greenhouse`, `lever`, `ashby`, `remotive`) and **paginated search APIs** (`jsearch`, `adzuna`, `themuse`, `usajobs`). Unapplied (`status = 'new'`) rows from full-board sources are marked closed immediately if absent from a healthy scrape—while any company board that experienced an HTTP timeout or non-200 response during Stage 1 is added to `failed_companies` and exempted from expiration so transient network failures never cause false positives. Paginated search sources apply a configurable 3-day grace window (`date_last_seen < NOW() - INTERVAL 3 DAY`).

### 4.2 Hybrid NLP Scoring Formula, Snippet Normalization & Experience Calibration
Pure keyword counters fail on synonyms and context, whereas pure dense embeddings can over-score generic boilerplate descriptions that lack mandatory core tools (e.g., SQL, PyTorch, or AWS). Stage 3 combines both signals:

$$
\text{Composite Score} = \text{clip}_{[0, 100]}\left(0.50 \times S_{\text{keyword}} + 0.50 \times S_{\text{semantic}} + \Delta_{\text{snippet}} + \Delta_{\text{exp}}\right)
$$

- **Weighted Keyword Rubric ($S_{\text{keyword}} \in [0, 100]$):** Evaluates 10 weighted technical categories defined in [`config/keywords.yaml`](config/keywords.yaml) (`Core ML / AI`, `Applied AI / LLMs`, `Python Ecosystem`, `SQL & Databases`, `ML Frameworks`, `Cloud Infrastructure`, `Data Engineering`, `Backend / APIs`, `Containers & CI/CD`, and `Experimentation`) using compiled word-boundary regular expressions (`\b...\b`).
- **768-d Transformer Semantic Similarity ($S_{\text{semantic}} \in [0, 100]$):** Encodes the active resume and each job description (`f"{title}\n{description_text}"`) into L2-normalized 768-dimensional vectors using `BAAI/bge-base-en-v1.5`, scaling raw cosine similarity ($\cos \theta$) into a calibrated $0\text{--}100$ distribution.
- **Snippet-Length Normalization ($\Delta_{\text{snippet}}$):** Aggregators like Adzuna return 500-character snippets rather than full 4,000-character descriptions (e.g., Rank #7 *Nityo Infotech* in the sample digest above, where `Keyword = 50.0` and `Semantic = 70.9` yield a normalized `71.6` composite score). When `len(description_text) < 600` (`is_snippet=True`), the scorer shifts weight toward dense semantic similarity ($0.35 \times S_{\text{keyword}} + 0.65 \times S_{\text{semantic}}$) and applies a $+8.0$ truncation compensation offset so concise snippets compete fairly with full ATS descriptions.
- **Experience-Tier Calibration ($\Delta_{\text{exp}}$):** [`extract_min_years_experience`](src/scoring/filters.py) parses minimum required years across patterns like `3+ years`, `2-4 yrs`, and `at least 5 years`:
  - **Primary Target (`1–3 yrs` or Unspecified Non-Senior):** $\Delta_{\text{exp}} = 0.0$
  - **Stretch Tier (`4–5 yrs` or `Senior` title with unspecified years):** $\Delta_{\text{exp}} = -3.0$
  - **Hard Knockout (`6+ yrs` minimum required):** Rejected at Stage 1 and marked `status = 'rejected'` at Stage 3.

### 4.3 Dynamic `.pdf` / `.docx` Resume Discovery & SHA-256 Change Detection
Users can drop any `.pdf`, `.docx`, `.txt`, or `.md` resume into [`data/`](data/).
- [`discover_resume_file`](src/scoring/embed_scorer.py) prioritizes `.pdf` and `.docx` files over fallback `.txt` files, extracting text via `pypdf` and `python-docx`.
- On every run, [`run_job_matcher.ps1`](run_job_matcher.ps1) computes the SHA-256 file hash of the active resume and compares it against `data/.last_resume_hash`. Whenever the resume file is modified or replaced, the runner automatically injects `--rescore` to recompute all database embeddings and composite scores against the updated resume.

### 4.4 State-Aware Two-Way Notion CRM Synchronization
[`src/exporters/notion_sync.py`](src/exporters/notion_sync.py) connects directly to the Notion REST API (`2022-06-28`):
- **Automatic Schema Provisioning & Child-Database Resolution:** Accepts either a raw 32-character Notion database ID or a full parent page URL, resolves inline `child_database` blocks automatically, and provisions all 14 tracker columns (`Company`, `Match Score`, `Keyword Score`, `Semantic Score`, `Experience Tier`, `Workplace`, `Status`, `Date Applied`, `Follow-Up Date`, `Contact / Notes`, `Salary`, `Source`, `Location`, `URL`).
- **State-Aware Expiration Cleanup (`archive_expired_notion_pages`):** Before archiving an expired job's Notion page, the exporter queries `GET /v1/pages/{page_id}` to inspect the user's current `Status` property on the Notion board:
  - Cards still sitting in **`New Match`** or **`Want to Apply`** are archived (`PATCH {"archived": true}`) so the user's **`To Apply Queue`** only contains live openings.
  - Cards moved by the user to **`Applied`**, **`Follow Up Needed`**, **`Interviewing`**, **`Offer`**, or **`Declined / Rejected`** are **preserved on the Notion board** (and updated to `status = 'applied'` in MySQL) so application history is never lost after a company closes the listing.

---

## 5. Repository Directory Tree

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

## 6. Quickstart & Installation Guide

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