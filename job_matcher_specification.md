# Job Matcher Application Specification (`SPEC.md`)

## 0. Project Context & Environment
* **Workspace Root:** `C:\Users\jenge\OneDrive\Documents\job-matcher`
* **IDE & Agent:** Microsoft Visual Studio 2026 with Google Antigravity
* **Python Environment:** Conda environment `job-matcher` (Python 3.11)
* **Resume Source:** External `data/resume.txt` (loaded dynamically at runtime) and `config/keywords.yaml` (for zero-code weight tuning).
* **Primary Target Titles:** Data Scientist, AI Engineer
* **Accepted Title Variations:** Machine Learning Engineer, Applied AI Engineer, Data Engineer, ML Ops Engineer, GenAI Engineer
* **Excluded Title Prefixes (Knockout):** Staff, Principal, Director, VP, Vice President, Head of, Intern, Internship, Lead, Manager
* **Location & Commute Rules:**
  * **Mode 1 (Remote):** US-eligible Remote (`Remote`, `Remote - US`, `Anywhere`, `Distributed`, `Work from Home`, `WFH`; exclude `EMEA`, `APAC`, `UK Only`, `Canada Only`, `Europe Only`, `India`, `LATAM`).
  * **Mode 2 (Local $\le$ 25 Min Commute):** ~15-mile radius around the Raleigh/RTP area (`Raleigh`, `Cary`, `Morrisville`, `RTP`, `Research Triangle`, `Garner`, `Apex`, `Durham`, `Wake`).

---

## 1. Stage 1: Ingestion Layer (`src/ingest/`)
* **Goal:** Poll structured JSON job feeds daily without triggering anti-bot blocks.
* **Selected Sources:**
  * **Source A (Direct ATS Poller — `src/ingest/ats_poller.py`):**
    * Greenhouse Public API: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
    * Lever Public API: `https://api.lever.co/v0/postings/{slug}?mode=json`
    * Configured via `config/companies.yaml`.
  * **Source B (Broad Local + Remote Aggregator — `src/ingest/adzuna_client.py`):**
    * Adzuna API (`ADZUNA_APP_ID` and `ADZUNA_API_KEY` from `.env`).
    * Query 1 (Local): `location="Raleigh, NC"`, `distance=24` (km) for target titles.
    * Query 2 (Remote): `what="Data Scientist" OR "AI Engineer" remote`.

---

## 2. Stage 2: Normalization & Storage Layer (`src/db/`)
* **Database Engine:** MySQL 8.0+ (`mysql+pymysql://user:pass@localhost:3306/job_matcher`) managed via SQLAlchemy 2.0 ORM (`DeclarativeBase`, `mapped_column`).
* **Deduplication Strategy:**
  * Deterministic `job_hash` = SHA-256(`lowercase_alphanumeric(company) | lowercase_alphanumeric(title) | lowercase(workplace_type)`).
  * Upsert via MySQL `INSERT ... ON DUPLICATE KEY UPDATE date_last_seen = NOW()`.
  * Only rows where `match_score IS NULL` are passed to Stage 3 for scoring.
* **Table Schema (`job_postings`):**
  * `job_hash` (`CHAR(64)`, Primary Key) — SHA-256 composite deduplication key
  * `source` (`VARCHAR(32)`, NOT NULL) — `greenhouse`, `lever`, `adzuna`
  * `external_id` (`VARCHAR(128)`, NULL) — Source-specific posting ID
  * `company` (`VARCHAR(128)`, NOT NULL, INDEX)
  * `title` (`VARCHAR(255)`, NOT NULL)
  * `location_raw` (`VARCHAR(255)`, NULL)
  * `workplace_type` (`ENUM('Remote', 'Local', 'Hybrid')`, NOT NULL, INDEX)
  * `salary_min` (`INT`, NULL)
  * `salary_max` (`INT`, NULL)
  * `url` (`VARCHAR(768)`, UNIQUE, NOT NULL)
  * `description_text` (`LONGTEXT`, NOT NULL) — HTML-stripped plain text
  * `date_posted` (`DATETIME`, NULL)
  * `date_fetched` (`DATETIME`, NOT NULL, DEFAULT `CURRENT_TIMESTAMP`)
  * `date_last_seen` (`DATETIME`, NOT NULL, DEFAULT `CURRENT_TIMESTAMP` ON UPDATE `CURRENT_TIMESTAMP`)
  * `keyword_score` (`FLOAT`, NULL) — Deterministic rubric score ($0$ to $100$)
  * `semantic_score` (`FLOAT`, NULL) — Local embedding similarity score ($0$ to $100$)
  * `match_score` (`FLOAT`, NULL, INDEX) — Weighted composite score ($0$ to $100$; `NULL` = unscored)
  * `matched_keywords` (`JSON`, NULL) — Breakdown of matched Tier 1–3 groups and missing job keywords
  * `status` (`ENUM('new', 'reviewed', 'applied', 'rejected')`, DEFAULT `'new'`, INDEX)
  * `notion_page_id` (`VARCHAR(64)`, NULL) — Reserved for Phase 2 Notion sync tracking

---

## 3. Stage 3: Filtering & Hybrid Scoring Engine (`src/scoring/`)
* **Step 3.1: Hard Knockout Filters (`src/scoring/filters.py`):**
  1. Reject if `title` matches excluded seniority prefixes (`Staff|Principal|Director|VP|Intern|Lead|Manager`).
  2. Reject if `title` does not match target role family (`Data Scien|AI Engineer|Machine Learning|ML Engineer|Applied AI|Data Engineer|MLOps|GenAI`).
  3. Reject if `location_raw` fails both the Local Commute Whitelist and the US-Remote Whitelist.
* **Step 3.2: Deterministic Keyword Rubric (`src/scoring/keyword_scorer.py` — 50% Weight, $0\text{--}100$ pts):**
  * Configured in `config/keywords.yaml`. Each group within a tier can trigger at most once per posting.
  * **Tier 1: Core Pillars ($+15$ pts per group, max $60$ pts):**
    1. *Python Ecosystem:* `python`, `pandas`, `numpy`
    2. *SQL & Databases:* `sql`, `mysql`, `postgres`, `postgresql`, `relational database`
    3. *Cloud Infrastructure:* `aws`, `amazon web services`, `s3`, `ec2`, `rds`, `sagemaker`, `bedrock`, `cloud`
    4. *Core ML / AI:* `machine learning`, `deep learning`, `artificial intelligence`, `statistical modeling`
  * **Tier 2: High-Value Differentiators ($+10$ pts per group, max $30$ pts):**
    1. *Data Engineering:* `etl`, `data pipeline`, `data engineering`, `airflow`, `spark`, `pyspark`, `data warehouse`, `dbt`
    2. *Applied AI / LLMs:* `llm`, `large language model`, `rag`, `retrieval augmented`, `embeddings`, `vector database`, `genai`, `generative ai`, `agent`
    3. *ML Frameworks:* `pytorch`, `tensorflow`, `scikit-learn`, `sklearn`, `xgboost`, `huggingface`, `transformers`
    4. *Backend / APIs:* `fastapi`, `flask`, `rest api`, `microservices`, `sqlalchemy`
  * **Tier 3: Credentials & DevOps Bonuses ($+5$ pts per group, max $10$ pts):**
    1. *Graduate Degree:* `master's`, `ms in`, `m.s.`, `phd`, `ph.d.`, `graduate degree`
    2. *Containers & CI/CD:* `docker`, `kubernetes`, `git`, `ci/cd`, `github actions`, `linux`
    3. *Experimentation:* `a/b testing`, `causal inference`, `hypothesis testing`, `time series`, `bayesian`
* **Step 3.3: Local Embedding Similarity (`src/scoring/embed_scorer.py` — 50% Weight, $0\text{--}100$ pts):**
  * Encodes `data/resume.txt` and `description_text` locally using `sentence-transformers` (`BAAI/bge-small-en-v1.5`) and computes cosine similarity scaled to $0\text{--}100$.
* **Step 3.4: Composite Score Formula:**
  $$\text{match\_score} = 0.50 \times \text{keyword\_score} + 0.50 \times \text{semantic\_score}$$

---

## 4. Stage 4: Output & Review Layer (`src/exporters/`)
* **Phase 1 Default (Option C — Local CLI + Markdown Digest in `src/exporters/markdown_digest.py`):**
  * Query `job_postings WHERE match_score >= 65 AND status = 'new' ORDER BY match_score DESC`.
  * Render a formatted terminal summary table via `rich`.
  * Export a daily Markdown report to `reports/daily_matches_YYYY-MM-DD.md` containing clickable links, salary ranges, `[REMOTE]` vs. `[LOCAL]` badges, composite score breakdown, and matched/missing skill tags.
* **Phase 2 Extension (Option A — Pluggable Notion Board Sync in `src/exporters/notion_sync.py`):**
  * Activated automatically when `NOTION_API_KEY` and `NOTION_DATABASE_ID` are set in `.env`.
  * Pushes new high-scoring rows to a Notion database and records the returned `notion_page_id` in MySQL to prevent duplicate cards.

---

## 5. Project Directory Structure
```text
job-matcher/
├── SPEC.md
├── .agents/
│   └── rules.md               # Persistent workspace instructions for Antigravity
├── .env.example
├── .gitignore
├── requirements.txt
├── config/
│   ├── keywords.yaml          # Tier 1-3 regex weights & tracked skill groups
│   └── companies.yaml         # Target Greenhouse & Lever company slugs
├── data/
│   └── resume.txt             # Plain-text resume (loaded at runtime)
├── reports/                   # Generated daily_matches_YYYY-MM-DD.md digests
└── src/
    ├── __init__.py
    ├── main.py                # CLI orchestrator (ingest -> store -> score -> export)
    ├── db/
    │   ├── __init__.py
    │   ├── models.py          # SQLAlchemy 2.0 MySQL schema (JobPosting)
    │   └── session.py         # Engine initialization, session factory & upsert helpers
    ├── ingest/
    │   ├── __init__.py
    │   ├── ats_poller.py      # Greenhouse & Lever JSON fetchers
    │   └── adzuna_client.py   # Adzuna local radius & remote fetcher
    ├── scoring/
    │   ├── __init__.py
    │   ├── filters.py         # Title & 25-min commute / remote regex knockouts + SHA-256 hash
    │   ├── keyword_scorer.py  # Tier 1-3 YAML rubric engine
    │   └── embed_scorer.py    # Local bge-small-en-v1.5 cosine similarity scorer
    └── exporters/
        ├── __init__.py
        ├── markdown_digest.py # Phase 1: Rich CLI table + Markdown report generator
        └── notion_sync.py     # Phase 2: Optional Notion API integration
```

---

## 6. Incremental Implementation Checklist
- [x] **Step 0:** Initialize `C:\Users\jenge\OneDrive\Documents\job-matcher` and activate Conda environment `job-matcher`.
- [x] **Step 1:** Create `requirements.txt`, `config/companies.yaml`, `src/scoring/filters.py`, and `src/ingest/ats_poller.py`; verify live terminal job fetching.
- [ ] **Step 2:** Create `.env`, `src/db/models.py`, and `src/db/session.py`; verify MySQL 8.0 table creation and `job_hash` deduplication upsert.
- [ ] **Step 3:** Create `config/keywords.yaml`, `data/resume.txt` (placeholder until updated), `src/scoring/keyword_scorer.py`, and `src/scoring/embed_scorer.py`.
- [ ] **Step 4:** Create `src/exporters/markdown_digest.py` and `src/main.py` to run the complete pipeline end-to-end.