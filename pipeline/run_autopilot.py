"""
Point d'entrée CLI du pipeline Pinterest autonome (GitHub Actions / local).
Modes :
  --demo-render : génère les 3 nouvelles épingles de démonstration (1000x1500) + planche d'aperçu, sans publication.
  --dry-run     : exécute l'orchestrateur complet en simulation (aucune écriture Buffer).
  --live        : exige ALLOW_LIVE_SCHEDULE=1 ET BUFFER_API_TOKEN (non utilisé dans cette mission).
"""
import argparse
import json
import os
import sys
from PIL import Image

from .buffer_client import BufferGraphQLClient
from .content_engine import build_utm_url, render_pin_png, validate_destination_url, validate_license
from .metrics_sync import fetch_d1_telemetry
from .orchestrator import run_daily_replenish

PIPELINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(PIPELINE_ROOT)
CATALOG_PATH = os.path.join(PIPELINE_ROOT, "catalog", "guides_catalog.json")
STATE_PATH = os.path.join(PIPELINE_ROOT, "state", "published_history.json")
FONT_PATH = os.path.join(REPO_ROOT, "tiktok-cloud-pipeline", "assets", "DejaVuSans-Bold.ttf")
OUT_DEMO_DIR = os.path.join(PIPELINE_ROOT, "out", "demo_pins")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def run_demo_render():
    catalog = load_json(CATALOG_PATH, {"candidates": []})
    os.makedirs(OUT_DEMO_DIR, exist_ok=True)
    thumbs = []
    results = []
    for cand in catalog["candidates"]:
        lic_ok, lic_msg = validate_license(cand.get("media"))
        assert lic_ok, f"Licence invalide pour {cand['id']}: {lic_msg}"
        dest = build_utm_url(cand["destination_url"], cand["utm_campaign"], cand.get("anchor"))
        url_ok, url_msg = validate_destination_url(dest, anchor=cand.get("anchor"))
        assert url_ok, f"Destination invalide pour {cand['id']}: {url_msg}"

        out_png = os.path.join(OUT_DEMO_DIR, f"{cand['id']}-1000x1500.png")
        info = render_pin_png(cand, REPO_ROOT, FONT_PATH, out_png)
        info["id"] = cand["id"]
        info["destination_url"] = dest
        info["license"] = cand["media"]["license"]
        info["size_kb"] = round(os.path.getsize(out_png) / 1024, 1)
        results.append(info)
        thumbs.append(Image.open(out_png).resize((333, 500), Image.LANCZOS))
        print(f"DEMO_PIN_OK: {cand['id']} {info['width']}x{info['height']} {info['size_kb']} KB HTTP_200_AND_ANCHOR_OK")

    sheet = Image.new("RGB", (333 * len(thumbs), 500), (12, 14, 18))
    for i, th in enumerate(thumbs):
        sheet.paste(th, (i * 333, 0))
    sheet_path = os.path.join(OUT_DEMO_DIR, "pinterest-03-demo-3-pins-preview.jpg")
    sheet.save(sheet_path, quality=90)
    with open(os.path.join(OUT_DEMO_DIR, "demo_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print("DEMO_SHEET_OK:", sheet_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo-render", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--metrics", action="store_true")
    args = parser.parse_args()

    if args.demo_render:
        run_demo_render()
        return

    if args.metrics:
        print(json.dumps(fetch_d1_telemetry(), indent=2, ensure_ascii=False))
        return

    dry_run = not (args.live and os.environ.get("ALLOW_LIVE_SCHEDULE") == "1")
    token = os.environ.get("BUFFER_API_TOKEN", "")
    channel_id = os.environ.get("BUFFER_PINTEREST_CHANNEL_ID", "6b0854348541f09ecbb1427e")
    image_base_url = os.environ.get("PIN_IMAGE_BASE_URL", "https://example.invalid/pins")

    catalog = load_json(CATALOG_PATH, {"candidates": []})
    state = load_json(STATE_PATH, {"last_scheduled_day": None, "history": []})
    client = BufferGraphQLClient(token=token)

    log, new_state = run_daily_replenish(
        buffer_client=client,
        catalog=catalog,
        state=state,
        channel_id=channel_id,
        image_base_url=image_base_url,
        dry_run=dry_run,
    )
    os.makedirs(os.path.join(PIPELINE_ROOT, "out"), exist_ok=True)
    with open(os.path.join(PIPELINE_ROOT, "out", "run_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
    print(json.dumps(log, indent=2, ensure_ascii=False))
    if not dry_run and log["scheduled_count"] > 0:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(new_state, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
