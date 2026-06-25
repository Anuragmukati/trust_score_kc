import sys 
import json
import logging
import yaml
import gspread
import google.auth
from datetime import datetime, timezone
from google.cloud import storage, bigquery, dataplex_v1
from google.protobuf.json_format import MessageToDict    # Safely parses nested structural payloads

# Configure logging format and level
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

logger.info("Initializing application and loading configurations...")

# Load configuration from config.yaml
try:
    with open("config.yaml", "r") as f:
        config_yaml = yaml.safe_load(f) or {}
    logger.info("Successfully loaded config.yaml")
except Exception as e:
    logger.warning(f"Could not load config.yaml, relying on default values. Error: {e}")
    config_yaml = {}

PROJECT_ID = config_yaml.get("PROJECT_ID", "dmgcp-del-181")
LOCATION = config_yaml.get("LOCATION", "us-central1") # Standardized fallback matching templates location
CONFIG_BUCKET = config_yaml.get("CONFIG_BUCKET")
DQ_DATASET = config_yaml.get("DQ_DATASET", "udm_prd_tbls")
DQ_TABLE = config_yaml.get("DQ_TABLE", "central_dq_results")
SPREADSHEET_ID = config_yaml.get("SPREADSHEET_ID")
GOVERNANCE_ASPECT_TYPE = config_yaml.get("GOVERNANCE_ASPECT_TYPE")
TRUST_ASPECT_TYPE = config_yaml.get("TRUST_ASPECT_TYPE")

logger.info("Initializing GCP clients (Storage, BigQuery, Dataplex)...")
storage_client = storage.Client(project=PROJECT_ID)
bq_client = bigquery.Client(project=PROJECT_ID)
dataplex_client = dataplex_v1.CatalogServiceClient()

def get_aspect_map_key(full_aspect_resource):
    """Converts 'projects/A/locations/B/aspectTypes/C' to the API map key 'A.B.C'"""
    parts = full_aspect_resource.split('/')
    if len(parts) == 6:
        return f"{parts[1]}.{parts[3]}.{parts[5]}"
    return full_aspect_resource

# Generate the correct map keys required by the Dataplex dictionary
TRUST_MAP_KEY = get_aspect_map_key(TRUST_ASPECT_TYPE)

def get_trust_level(score, domain, config):
    if score is None:
        return "Unknown"
        
    thresholds = config.get('domain_benchmarks', {}).get(domain) or config.get('domain_benchmarks', {}).get('default', {})
    if not thresholds or 'high' not in thresholds or 'medium' not in thresholds:
        return "Unknown"
        
    if score >= thresholds['high']: return "High"
    if score >= thresholds['medium']: return "Medium"
    return "Low"

def get_bq_entry_name(project, dataset, table):
    return f"projects/{project}/locations/{LOCATION}/entryGroups/@bigquery/entries/bigquery.googleapis.com/projects/{project}/datasets/{dataset}/tables/{table}"

def update_trust_scores():
    logger.info("=== STARTING TRUST SCORE UPDATE PROCESS ===")
    try:
        # 1. Fetch DQ Thresholds Config
        logger.info(f"Step 1: Fetching DQ Thresholds from bucket: {CONFIG_BUCKET}")
        bucket = storage_client.bucket(CONFIG_BUCKET)
        config_blob = bucket.blob('dq_thresholds.json').download_as_string()
        dq_config = json.loads(config_blob)
        logger.info("Successfully loaded dq_thresholds.json into memory.")

        # 2. Authenticate and Read Google Spreadsheet
        logger.info("Step 2: Authenticating with Google Sheets API...")
        credentials, _ = google.auth.default(scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ])
        gc = gspread.authorize(credentials)
        
        logger.info(f"Fetching records from Spreadsheet ID: {SPREADSHEET_ID}")
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        records = sheet.get_all_records()
        logger.info(f"Successfully retrieved {len(records)} rows from the spreadsheet.")

        # 3. Fetch all latest BQ scores at once
        logger.info(f"Step 3: Fetching all latest DQ scores from {PROJECT_ID}.{DQ_DATASET}.{DQ_TABLE}...")
        query = f"""
            SELECT 
                project_id, 
                dataset_id, 
                table_name, 
                overall_dq_score, 
                execution_timestamp
            FROM (
              SELECT 
                  data_source.table_project_id AS project_id, 
                  data_source.dataset_id AS dataset_id, 
                  data_source.table_id AS table_name, 
                  job_quality_result.score AS overall_dq_score, 
                  job_end_time AS execution_timestamp,
                  ROW_NUMBER() OVER(
                      PARTITION BY data_source.table_project_id, data_source.dataset_id, data_source.table_id 
                      ORDER BY job_end_time DESC
                  ) as rn
              FROM `{PROJECT_ID}.{DQ_DATASET}.{DQ_TABLE}`
              WHERE data_source.table_id IS NOT NULL
            )
            WHERE rn = 1
        """
        bq_results = bq_client.query(query).result()
        
        dq_memory_dict = {}
        for row in bq_results:
            key = f"{row.project_id}.{row.dataset_id}.{row.table_name}"
            dq_memory_dict[key] = {
                "score": row.overall_dq_score,
                "raw_timestamp": row.execution_timestamp
            }
        logger.info(f"Loaded {len(dq_memory_dict)} unique table scores into memory.")

        # 4. Iterate through spreadsheet and apply Aspects
        logger.info("Step 4: Iterating through spreadsheet records to apply Trust Scores...")
        success_count = 0
        error_count = 0

        for index, row in enumerate(records):
            t_project = row.get("table_project_id")
            t_dataset = row.get("dataset_id")
            t_table = row.get("table_id")
            
            logger.info(f"--- Processing row {index + 1}/{len(records)}: {t_project}.{t_dataset}.{t_table} ---")

            if not all([t_project, t_dataset, t_table]):
                logger.warning(f"Row {index + 1} skipped: Missing required project, dataset, or table information. Data: {row}")
                error_count += 1
                continue

            entry_name = get_bq_entry_name(t_project, t_dataset, t_table)
            dict_key = f"{t_project}.{t_dataset}.{t_table}"

            try:
                # 5. Fetch Dataplex Entry
                logger.info(f"Fetching Dataplex entry for {t_table}...")
                
                # FIXED: Swapped EntryView.FULL to EntryView.ALL to force the API 
                # to return populated field data for optional/non-required aspects.
                request_entry = dataplex_v1.GetEntryRequest(
                    name=entry_name,
                    view=dataplex_v1.EntryView.ALL
                )
                entry = dataplex_client.get_entry(request=request_entry)
                
                domain = "default"
                logger.info(f"Attached Aspect Keys found: {list(entry.aspects.keys())}")
                
                for aspect_map_key, aspect_obj in entry.aspects.items():
                    if "data-governance" in aspect_map_key.lower():
                        logger.info(f"Successfully located Governance metadata under key: {aspect_map_key}")
                        
                        # Unpack the raw payload layer securely
                        try:
                            fields_dict = MessageToDict(aspect_obj._pb.data) if aspect_obj._pb.data else {}
                        except Exception as parse_err:
                            logger.warning(f"MessageToDict parsing fallback active. Error: {parse_err}")
                            fields_dict = {k: v for k, v in aspect_obj.data.items()}
                        
                        logger.info(f"Extracted Governance fields successfully: {fields_dict}")
                        
                        # Dynamically search the sanitized key contents
                        for field_key, field_value in fields_dict.items():
                            sanitized_key = field_key.lower().replace("-", "").replace("_", "").replace(" ", "")
                            if sanitized_key == "datadomain":
                                domain = field_value
                                logger.info(f"Found domain context '{domain}' associated with {t_table}.")
                                break
                        break

                if domain == "default":
                    logger.warning(f"Could not discover an assigned domain field inside Governance data for {t_table}. Using 'default'.")
                
                # 6. Dictionary lookup
                dq_data = dq_memory_dict.get(dict_key)
                if dq_data:
                    score = dq_data["score"]
                    last_evaluated = dq_data["raw_timestamp"].astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                    logger.info(f"Found matching DQ result in memory: Score {score}, Last Evaluated {last_evaluated}.")
                else:
                    score = None
                    last_evaluated = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                    logger.warning(f"No DQ result found in memory for {dict_key}. Score set to None.")

                # Calculate Tier
                trust_level = get_trust_level(score, domain, dq_config)
                logger.info(f"Calculated Trust Tier: {trust_level}")

                # 7. Apply Aspect
                logger.info(f"Applying Aspect '{TRUST_ASPECT_TYPE}' to entry...")
                
                # FIXED: Instantiating arguments inside constructor cleanly assigns internal Struct values
                new_aspect = dataplex_v1.Aspect(
                    aspect_type=TRUST_ASPECT_TYPE,
                    data={
                        "trust-score": trust_level,
                        "last-evaluated": last_evaluated
                    }
                )

                # Create isolated Entry container to target strictly our updated aspect
                update_entry_obj = dataplex_v1.Entry()
                update_entry_obj.name = entry.name
                update_entry_obj.aspects[TRUST_MAP_KEY] = new_aspect
                
                request_update = dataplex_v1.UpdateEntryRequest(
                    entry=update_entry_obj,
                    update_mask={"paths": ["aspects"]},
                    aspect_keys=[TRUST_MAP_KEY]
                )
                
                dataplex_client.update_entry(request=request_update)
                logger.info(f"SUCCESS: Applied {trust_level} Trust Score to {t_table}.")
                success_count += 1

            except Exception as e:
                logger.exception(f"ERROR processing table {t_table}: {e}")
                error_count += 1

        logger.info(f"=== PROCESS COMPLETE. Successes: {success_count}, Errors: {error_count} ===")
        
        if error_count > 0:
            logger.error("Job completed but encountered errors. Failing the job execution.")
            sys.exit(1) 
        else:
            sys.exit(0)

    except Exception as e:
        logger.exception(f"CRITICAL ERROR executing trust score update: {e}")
        sys.exit(1) 

if __name__ == "__main__":
    logger.info("Executing script as a standalone job...")
    update_trust_scores()