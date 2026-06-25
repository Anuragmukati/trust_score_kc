# Trust Score Automation

This service automates the process of calculating and attaching "Trust Scores" to BigQuery tables within the Google Cloud Dataplex Catalog. It acts as a bridge between raw Data Quality (DQ) results and the metadata visible to data consumers.

## What it does

Data Quality scores are often buried in BigQuery tables that users never see. This script:
1.  **Identifies Target Tables:** Reads a curated list of tables from a Google Spreadsheet.
2.  **Fetches DQ Metrics:** Pulls the latest quality scores from a central BigQuery results table.
3.  **Contextualizes Quality:** Looks at the table's "Domain" (e.g., Finance vs. Marketing) stored in Dataplex to determine which quality thresholds to apply.
4.  **Publishes Metadata:** Updates the Dataplex Entry for that table with a human-readable Trust Score (High, Medium, Low) and a timestamp.

## How it works

The application follows a linear pipeline:

1.  **Config & Thresholds:** It loads `dq_thresholds.json` from GCS. This file defines what constitutes a "High" or "Medium" score for different business domains.
2.  **The Registry:** It connects to a Google Sheet (using `gspread`) to get the list of BQ tables (`project`, `dataset`, `table_id`) that need trust scoring.
3.  **BigQuery Lookup:** It runs a Window Function query against the central DQ table to grab the most recent execution result for every table in the registry.
4.  **Dataplex Enrichment:**
    *   For each table, it fetches the current Dataplex Entry.
    *   It scans existing **Governance Aspects** to find the `DataDomain`.
    *   It maps the BQ score against the domain-specific thresholds.
    *   It pushes a **Trust Aspect** back to Dataplex containing the tier and the evaluation time.

## System Flow

```mermaid
graph TD
    A[Google Sheet: Table Registry] --> Script[Score Processor Script]
    B[GCS: Domain Thresholds] --> Script
    C[BigQuery: Data Quality Metrics] --> Script
    Script --> D[Dataplex: Fetch Metadata]
    D --> E[Determine Domain Logic]
    E --> F[Calculate Tier]
    F --> G[Dataplex: Attach Trust Score]
```

## Configuration

The behavior is controlled via `config.yaml`. Key parameters include:
*   `DQ_TABLE`: The source of truth for your quality metrics.
*   `SPREADSHEET_ID`: The registry of tables to process.
*   `TRUST_ASPECT_TYPE`: The resource name of the Dataplex Aspect Type used for the score.
*   `CONFIG_BUCKET`: The GCS bucket containing `dq_thresholds.json`.

## Prerequisites

*   **Service Account:** A custom SA is required for production.
*   **Google Sheets API:** The service account must have access to the Google Sheet defined in the config.
*   **Dataplex Aspects:** The `DataDomain` must be populated in a Governance-related aspect for domain-specific scoring to work; otherwise, it defaults to a standard benchmark.

## Setup & Authentication

### 1. IAM Roles
Assign the following roles to your Service Account:
- `roles/bigquery.dataViewer` & `roles/bigquery.jobUser`
- `roles/storage.objectViewer`
- `roles/dataplex.catalogEditor`

### 2. Google Sheets Access
You must **Share** the registry spreadsheet with the Service Account's email address (e.g., `service-account@project.iam.gserviceaccount.com`) to allow `gspread` to read the table list.

### 3. Local Development
Set the environment variable to point to your key file:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="path/to/key.json"
```

## Logic Breakdown: Scoring

Scores are categorized based on the `dq_thresholds.json` mapping:

| Score Range (Example) | Tier |
| :--- | :--- |
| >= 0.95 | **High** |
| >= 0.80 | **Medium** |
| < 0.80 | **Low** |
| No Result | **Unknown** |

## Execution

The script is designed to run as a standalone job. To deploy it as a **Cloud Run Job** using your project's default App Engine Service Account (App SA):

```bash
gcloud run jobs deploy trust-score-job \
  --source . \
  --project [Project_ID] \
  --service-account="Service_Account" \
  --region="us-central1"
```
*Note: Ensure the App SA has been granted the IAM roles listed in the Setup section and has "Viewer" access to the Registry Google Sheet.*
