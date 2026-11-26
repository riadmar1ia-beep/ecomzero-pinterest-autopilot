"""
Point d'entrée CLI du pipeline Pinterest autonome (GitHub Actions / local).
Modes :
  --demo-render      : génère les 3 épingles de démonstration (1000x1500), sans publication.
  --renew-catalog    : renouvelle catalog/guides_catalog.json et génère les nouveaux PNG si réserve < 3.
  --dry-run          : exécute l'orchestrateur en simulation (aucune écriture Buffer).
  --live             : exige ALLOW_LIVE_SCHEDULE=1 ET BUFFER_API_TOKEN.
  --metrics-snapshot : collecte D1 + statuts Buffer (sans inventer de données Pinterest/Travelpayouts).

Codes de sortie :
  0 : exécution saine (SCHEDULED_LIVE, DRY_RUN_SCHEDULED, QUEUE_TARGET_MET, QUEUE_FULL_FREE_LIMIT, IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY)
  1 : erreur métier réelle (API_ERROR, QUOTA_EXHAUSTED, ERROR_BOARD_NOT_FOUND, NO_ELIGIBLE_CANDIDATE) -> échoue le job GitHub Actions.
"""
import argparse
import datetime
import json
import os
import sys
from PIL import Image

from .buffer_client import BufferApiError, BufferGraphQLClient
from .catalog_renewer import renew_catalog_if_needed
from .content_engine import build_utm_url, render_pin_png, validate_destination_url, validate_license
from .metrics_sync import fetch_d1_telemetry
from .orchestrator import run_daily_replenish

PIPELINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PIPELINE_ROOT)
CATALOG_PATH = os.path.join(PIPELINE_ROOT, "catalog", "guides_catalog.json")
TEMPLATES_PATH = os.path.join(PIPELINE_ROOT, "catalog", "guide_templates.json")
STATE_PATH = os.path.join(PIPELINE_ROOT, "state", "published_history.json")
FONT_PATH = os.path.join(PIPELINE_ROOT, "assets", "DejaVuSans-Bold.ttf")
OUT_PINS_DIR = os.path.join(PIPELINE_ROOT, "out", "pins")
OUT_DEMO_DIR = os.path.join(PIPELINE_ROOT, "out", "demo_pins")

HEALTHY_STATUSES = {
    "SCHEDULED_LIVE",
    "DRY_RUN_SCHEDULED",
    "QUEUE_TARGET_MET",
    "QUEUE_FULL_FREE_LIMIT",
    "IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY",
}


def exit_code_for_status(status):
    return 0 if status in HEALTHY_STATUSES else 1


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def run_renew_catalog(render_images=True):
    catalog = load_json(CATALOG_PATH, {"candidates": []})
    templates = load_json(TEMPLATES_PATH, {"templates": []})
    state = load_json(STATE_PATH, {"last_scheduled_day": None, "history": []})
    report, updated = renew_catalog_if_needed(
        catalog=catalog,
        templates_doc=templates,
        state=state,
        pipeline_root=PIPELINE_ROOT,
        font_path=FONT_PATH,
        out_pins_dir=OUT_PINS_DIR,
        render_images=render_images,
    )
    if report["added_ids"]:
        save_json(CATALOG_PATH, updated)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def run_metrics_snapshot():
    snap = {
        "captured_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "d1_telemetry": None,
        "buffer_queue_count": "N/A",
        "pinterest_impressions": "N/A (relevé manuel requis dans Pinterest Analytics)",
        "pinterest_outbound_clicks": "N/A (relevé manuel requis dans Pinterest Analytics)",
        "affiliate_bookings_and_commissions": "N/A (relevé manuel requis dans Travelpayouts / Trip Tours)",
    }
    try:
        snap["d1_telemetry"] = fetch_d1_telemetry()
    except Exception as e:
        snap["d1_telemetry"] = {"error": f"{type(e).__name__}: {e}"}

    token = os.environ.get("BUFFER_API_TOKEN", "")
    channel_id = os.environ.get("BUFFER_PINTEREST_CHANNEL_ID", "6b0854348541f09ecbb1427e")
    if token:
        try:
            client = BufferGraphQLClient(token=token)
            posts = client.list_scheduled_posts(channel_id)
            snap["buffer_queue_count"] = len(posts)
            snap["buffer_scheduled_ids"] = [p.get("id") for p in posts]
        except BufferApiError as e:
            snap["buffer_queue_count"] = f"ERROR: {e}"

    save_json(os.path.join(PIPELINE_ROOT, "out", "metrics_snapshot.json"), snap)
    print(json.dumps(snap, indent=2, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--renew-catalog", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--metrics-snapshot", action="store_true")
    args = parser.parse_args()

    if args.renew_catalog:
        run_renew_catalog(render_images=True)
        return

    if args.metrics_snapshot:
        run_metrics_snapshot()
        return

    dry_run = not (args.live and os.environ.get("ALLOW_LIVE_SCHEDULE") == "1")
    token = os.environ.get("BUFFER_API_TOKEN", "")
    channel_id = os.environ.get("BUFFER_PINTEREST_CHANNEL_ID", "6b0854348541f09ecbb1427e")
    image_base_url = os.environ.get(
        "PIN_IMAGE_BASE_URL",
        "https://raw.githubusercontent.com/riadmar1ia-beep/ecomzero-pinterest-autopilot/main/out/pins",
    )

    catalog = load_json(CATALOG_PATH, {"candidates": []})
    state = load_json(STATE_PATH, {"last_scheduled_day": None, "history": []})
    client = BufferGraphQLClient(token=token)

    log, new_state = run_daily_replenish(
        buffer_client=client,
        catalog=catalog,
        state=state,
        channel_id=channel_id,
        image_base_url=image_base_url,
        local_pins_dir=OUT_PINS_DIR,
        dry_run=dry_run,
    )
    save_json(os.path.join(PIPELINE_ROOT, "out", "run_log.json"), log)
    print(json.dumps(log, indent=2, ensure_ascii=False))

    if not dry_run and log["scheduled_count"] > 0:
        save_json(STATE_PATH, new_state)

    sys.exit(exit_code_for_status(log["status"]))


if __name__ == "__main__":
    main()
