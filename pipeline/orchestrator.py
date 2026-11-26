"""
Orchestrateur quotidien autonome pour alimenter la file Pinterest Buffer Free (PC éteint).
Règles métier :
- Réserve cible : QUEUE_TARGET = 7 épingles en attente.
- Plafond Buffer Free : QUEUE_MAX = 10 épingles programmées par canal.
- Cadence initiale : MAX_NEW_PER_RUN = 1 nouvelle épingle par exécution quotidienne.
- Idempotence : une réexécution le même jour (ou sur le même candidat) ne crée aucun doublon.
- Mode sécurisé : dry_run=True par défaut tant que ALLOW_LIVE_SCHEDULE != "1".
"""
import datetime
import json
import os

from .buffer_client import BufferApiError
from .content_engine import (
    build_utm_url,
    is_duplicate,
    validate_destination_url,
    validate_disclosure,
    validate_license,
)

QUEUE_TARGET = 7
QUEUE_MAX = 10
MAX_NEW_PER_RUN = 1


def compute_next_slot_utc(scheduled_posts, now_utc=None, hour_utc=18):
    """Choisit le lendemain à 18:00 UTC (19:00 CET) après la dernière épingle programmée."""
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    latest = now
    for p in scheduled_posts:
        due = p.get("dueAt")
        if due:
            try:
                dt = datetime.datetime.fromisoformat(due.replace("Z", "+00:00"))
                if dt > latest:
                    latest = dt
            except ValueError:
                pass
    next_day = (latest + datetime.timedelta(days=1)).replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return next_day.isoformat().replace("+00:00", "Z")


def run_daily_replenish(
    buffer_client,
    catalog,
    state,
    channel_id,
    board_name="Viajes y organización",
    image_base_url="https://raw.githubusercontent.com/OWNER/REPO/main/out/pins",
    dry_run=True,
    now_utc=None,
    url_fetcher=None,
):
    now = now_utc or datetime.datetime.now(datetime.timezone.utc)
    today_key = now.strftime("%Y-%m-%d")
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

    # Idempotence : maximum 1 exécution avec ajout par jour calendaire
    if state.get("last_scheduled_day") == today_key:
        log["status"] = "IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY"
        return log, state

    # 1. Lire la file Buffer et le tableau cible
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

    # 2. Respecter la cible (7) et le plafond Buffer Free (10)
    if queue_len >= QUEUE_MAX:
        log["status"] = "QUEUE_FULL_FREE_LIMIT"
        return log, state
    if queue_len >= QUEUE_TARGET:
        log["status"] = "QUEUE_TARGET_MET"
        return log, state

    # 3. Trier les candidats par priorité commerciale décroissante (offres APPROVED d'abord)
    history = state.get("history", [])
    candidates = sorted(
        catalog.get("candidates", []),
        key=lambda c: (len(c.get("affiliates_approved", [])) > 0, c.get("priority_score", 0)),
        reverse=True,
    )

    added = 0
    for cand in candidates:
        if added >= MAX_NEW_PER_RUN or (queue_len + added) >= min(QUEUE_TARGET, QUEUE_MAX):
            break

        # Contrôles qualité obligatoires avant toute programmation
        lic_ok, lic_msg = validate_license(cand.get("media"))
        if not lic_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": lic_msg})
            continue

        disc_ok, disc_msg = validate_disclosure(cand)
        if not disc_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": disc_msg})
            continue

        dup, dup_msg = is_duplicate(cand, history, scheduled_posts)
        if dup:
            log["rejected_candidates"].append({"id": cand["id"], "reason": dup_msg})
            continue

        dest_url = build_utm_url(cand["destination_url"], cand["utm_campaign"], cand.get("anchor"))
        url_ok, url_msg = validate_destination_url(dest_url, anchor=cand.get("anchor"), fetcher=url_fetcher)
        if not url_ok:
            log["rejected_candidates"].append({"id": cand["id"], "reason": url_msg})
            continue

        due_at = compute_next_slot_utc(scheduled_posts, now_utc=now)
        img_url = f"{image_base_url.rstrip('/')}/{cand['id']}.png"

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

        post_id = (res.get("post") or {}).get("id") or f"dryrun-{cand['id']}"
        entry = {
            "id": cand["id"],
            "buffer_post_id": post_id,
            "pin_title": cand["pin_title"],
            "destination_url": dest_url,
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
