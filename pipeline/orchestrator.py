"""
Orchestrateur quotidien autonome pour alimenter la file Pinterest Buffer Free (PC éteint).
Règles métier :
- Réserve cible : QUEUE_TARGET = 7 épingles en attente.
- Plafond Buffer Free : QUEUE_MAX = 10 épingles programmées par canal.
- Cadence initiale : MAX_NEW_PER_RUN = 1 nouvelle épingle par exécution quotidienne.
- Fuseau horaire : 19:00 Europe/Madrid (CET/CEST gérés automatiquement via zoneinfo).
- Idempotence : contrôle sur l'historique local ET les publications déjà présentes dans Buffer
  (protège contre un échec de commit GitHub après création réussie dans Buffer).
- Contrôle image : vérifie existence PNG locale, dimensions Pinterest, URL HTTPS publique HTTP 200 image/png.
- Mode sécurisé : dry_run=True par défaut ; jamais d'ID fictif en mode live.
"""
import datetime
import os
from zoneinfo import ZoneInfo

from .buffer_client import BufferApiError
from .content_engine import (
    build_utm_url,
    is_duplicate,
    validate_destination_url,
    validate_disclosure,
    validate_image_asset,
    validate_license,
)

QUEUE_TARGET = 7
QUEUE_MAX = 10
MAX_NEW_PER_RUN = 1
MADRID_TZ = ZoneInfo("Europe/Madrid")
TARGET_HOUR_MADRID = 19


def compute_next_slot_madrid_utc(scheduled_posts, now_utc=None, target_hour=TARGET_HOUR_MADRID):
    """
    Calcule le prochain créneau à 19:00 Europe/Madrid (CET ou CEST automatique),
    strictement après la date/heure de la dernière épingle déjà en file (et après maintenant).
    Retourne un horodatage ISO-8601 UTC (...Z).
    """
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=datetime.timezone.utc)

    latest_utc = now
    for p in scheduled_posts:
        due = p.get("dueAt")
        if due:
            try:
                dt = datetime.datetime.fromisoformat(due.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                if dt > latest_utc:
                    latest_utc = dt
            except ValueError:
                pass

    latest_madrid = latest_utc.astimezone(MADRID_TZ)
    next_date = latest_madrid.date() + datetime.timedelta(days=1)
    slot_madrid = datetime.datetime(
        next_date.year, next_date.month, next_date.day, target_hour, 0, 0, tzinfo=MADRID_TZ
    )
    slot_utc = slot_madrid.astimezone(datetime.timezone.utc)
    if slot_utc <= latest_utc:
        next_date = next_date + datetime.timedelta(days=1)
        slot_madrid = datetime.datetime(
            next_date.year, next_date.month, next_date.day, target_hour, 0, 0, tzinfo=MADRID_TZ
        )
        slot_utc = slot_madrid.astimezone(datetime.timezone.utc)
    return slot_utc.isoformat().replace("+00:00", "Z")


def run_daily_replenish(
    buffer_client,
    catalog,
    state,
    channel_id,
    board_name="Viajes y organización",
    image_base_url="https://raw.githubusercontent.com/riadmar1ia-beep/ecomzero-pinterest-autopilot/main/out/pins",
    local_pins_dir=None,
    dry_run=True,
    now_utc=None,
    url_fetcher=None,
    image_head_fetcher=None,
):
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    today_key = now.astimezone(MADRID_TZ).strftime("%Y-%m-%d")
    log = {
        "run_at_utc": now.isoformat().replace("+00:00", "Z"),
        "dry_run": dry_run,
        "status": "NOOP",
        "queue_before": None,
        "scheduled_count": 0,
        "scheduled_items": [],
        "rejected_candidates": [],
        "error": None,
    }

    if state.get("last_scheduled_day") == today_key:
        log["status"] = "IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY"
        return log, state

    # 1. Lire le tableau cible et les publications déjà programmées dans Buffer
    try:
        boards = buffer_client.list_pinterest_boards(channel_id)
        board = next((b for b in boards if b.get("name") == board_name), None)
        if not board:
            log["status"] = "ERROR_BOARD_NOT_FOUND"
            log["error"] = f"Tableau '{board_name}' introuvable sur le canal {channel_id}."
            return log, state
        scheduled_posts = buffer_client.list_scheduled_posts(channel_id)
    except BufferApiError as e:
        log["status"] = "QUOTA_EXHAUSTED" if e.is_quota else "API_ERROR"
        log["error"] = str(e)
        return log, state

    queue_len = len(scheduled_posts)
    log["queue_before"] = queue_len

    if queue_len >= QUEUE_MAX:
        log["status"] = "QUEUE_FULL_FREE_LIMIT"
        return log, state
    if queue_len >= QUEUE_TARGET:
        log["status"] = "QUEUE_TARGET_MET"
        return log, state

    history = list(state.get("history", []))
    candidates = sorted(
        catalog.get("candidates", []),
        key=lambda c: (len(c.get("affiliates_approved", [])) > 0, c.get("priority_score", 0)),
        reverse=True,
    )

    added = 0
    for cand in candidates:
        if added >= MAX_NEW_PER_RUN or (queue_len + added) >= min(QUEUE_TARGET, QUEUE_MAX):
            break

        lic_ok, lic_msg = validate_license(cand.get("media"))
        if not lic_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": lic_msg})
            continue

        disc_ok, disc_msg = validate_disclosure(cand)
        if not disc_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": disc_msg})
            continue

        # Contrôle doublon sur historique local ET publications déjà existantes dans Buffer
        dup, dup_msg = is_duplicate(cand, history, scheduled_posts)
        if dup:
            log["rejected_candidates"].append({"id": cand["id"], "reason": dup_msg})
            continue

        dest_url = build_utm_url(cand["destination_url"], cand["utm_campaign"], cand.get("anchor"))
        url_ok, url_msg = validate_destination_url(dest_url, anchor=cand.get("anchor"), fetcher=url_fetcher)
        if not url_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": url_msg})
            continue

        # Contrôle image PNG locale + URL HTTPS publique HTTP 200 image/png
        img_url = f"{image_base_url.rstrip('/')}/{cand['id']}.png"
        local_png = os.path.join(local_pins_dir, f"{cand['id']}.png") if local_pins_dir else None
        img_ok, img_msg = validate_image_asset(img_url, local_png_path=local_png, head_fetcher=image_head_fetcher)
        if not img_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": img_msg})
            continue

        due_at = compute_next_slot_madrid_utc(scheduled_posts, now_utc=now)

        try:
            res = buffer_client.schedule_pin(
                channel_id=channel_id,
                board_service_id=board["serviceId"],
                image_url=img_url,
                title=cand["pin_title"],
                description=cand["pin_description"],
                destination_url=dest_url,
                due_at_iso=due_at,
                dry_run=dry_run,
            )
        except BufferApiError as e:
            log["status"] = "QUOTA_EXHAUSTED" if e.is_quota else "API_ERROR"
            log["error"] = str(e)
            return log, state

        if dry_run:
            post_id = f"dryrun-{cand['id']}"
        else:
            post = res.get("post") if isinstance(res, dict) else None
            post_id = str((post or {}).get("id") or "").strip()
            if not post_id:
                log["status"] = "API_ERROR"
                log["error"] = "Mode live : identifiant Buffer absent après createPost (aucun ID fictif généré)."
                return log, state

        entry = {
            "id": cand["id"],
            "buffer_post_id": post_id,
            "pin_title": cand["pin_title"],
            "destination_url": dest_url,
            "image_url": img_url,
            "due_at_utc": due_at,
            "dry_run": dry_run,
        }
        history.append(entry)
        scheduled_posts.append({"id": post_id, "text": cand["pin_description"], "dueAt": due_at, "status": "scheduled"})
        log["scheduled_items"].append(entry)
        added += 1

    log["scheduled_count"] = added
    if added > 0:
        log["status"] = "DRY_RUN_SCHEDULED" if dry_run else "SCHEDULED_LIVE"
        state["last_scheduled_day"] = today_key
        state["history"] = history
    else:
        log["status"] = "NO_ELIGIBLE_CANDIDATE"
    return log, state
