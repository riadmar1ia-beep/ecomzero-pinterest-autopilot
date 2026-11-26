"""
Suite de tests complète (MISSION ECOM-ZERO-PINTEREST-03 — Étape 6).
Couvre les 9 scénarios exigés + garde-fou d'optimisation sur faible échantillon :
1. File vide (0 -> +1)
2. File partiellement remplie (5 -> +1)
3. File pleine / cible atteinte (7 et 10 -> 0)
4. Doublon (historique ou file Buffer active)
5. Erreur API (HTTP 500 / GraphQL error)
6. Lien invalide (HTTP 404 ou ancre HTML absente)
7. Licence manquante ou interdite (CC BY-SA)
8. Quota épuisé (HTTP 429)
9. Réexécution idempotente le même jour
"""
import copy
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline.buffer_client import BufferApiError, BufferGraphQLClient
from pipeline.metrics_sync import evaluate_topic_optimization
from pipeline.orchestrator import compute_next_slot_madrid_utc, run_daily_replenish

CATALOG = json.load(open(os.path.join(ROOT, "catalog", "guides_catalog.json"), encoding="utf-8"))
NOW = datetime.datetime(2026, 12, 2, 17, 0, 0, tzinfo=datetime.timezone.utc)


def ok_fetcher(url):
    return 200, '<h2 id="s-prefieres-no-hacerlo-tu">x</h2><h2 id="s-dos-recorridos-a-pie-desde-la-puerta-del-sol">y</h2>'


def ok_img_head(url):
    return 200, "image/png"


def make_fake_client(scheduled_posts, fail_mode=None, meta_fields=None):
    def transport(endpoint, payload, token):
        q = payload["query"]
        if fail_mode == "http_500":
            return 500, {"error": "Internal Server Error"}
        if fail_mode == "quota_429":
            return 429, {"error": "Rate limit exceeded"}
        if "organizations" in q:
            return 200, {"data": {"account": {"organizations": [{"id": "org_1", "name": "My organization"}]}}}
        if "PinterestMetadata" in q:
            return 200, {"data": {"channel": {"id": "ch1", "name": "ecomzeroshop", "metadata": {"boards": [{"id": "b1", "serviceId": "svc_board_1", "name": "Viajes y organización"}]}}}}
        if "status: [scheduled]" in q:
            return 200, {"data": {"posts": {"edges": [{"node": p} for p in scheduled_posts]}}}
        if "PinterestPostMetadataInput" in q:
            fields = meta_fields if meta_fields is not None else ["boardServiceId", "title", "url"]
            return 200, {"data": {"__type": {"inputFields": [{"name": f} for f in fields]}}}
        if "createPost" in q:
            if fail_mode == "mutation_quota":
                return 200, {"data": {"createPost": {"message": "Free plan limit of 10 posts reached"}}}
            if fail_mode == "empty_post":
                return 200, {"data": {"createPost": {}}}
            if fail_mode == "empty_id":
                return 200, {"data": {"createPost": {"post": {"id": "", "status": "scheduled"}}}}
            if fail_mode == "bad_status":
                return 200, {"data": {"createPost": {"post": {"id": "p_x", "status": "failed"}}}}
            return 200, {"data": {"createPost": {"post": {"id": "post_live_1", "status": "scheduled", "dueAt": payload["variables"]["input"]["dueAt"], "text": payload["variables"]["input"]["text"]}}}}
        return 200, {"data": {}}

    return BufferGraphQLClient(token="fake_secret_token_123", transport=transport)


def test_1_empty_queue_schedules_one_pin():
    client = make_fake_client([])
    state = {"last_scheduled_day": None, "history": []}
    log, new_state = run_daily_replenish(client, CATALOG, state, "ch1", dry_run=False, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log["status"] == "SCHEDULED_LIVE"
    assert log["queue_before"] == 0
    assert log["scheduled_count"] == 1
    assert log["scheduled_items"][0]["id"] == "demo-07-esim-vs-roaming-es"
    assert new_state["last_scheduled_day"] == "2026-12-02"


def test_2_partially_filled_queue_schedules_one_pin():
    existing = [{"id": f"p{i}", "text": f"Existing pin {i}", "dueAt": f"2026-12-0{i+1}T18:00:00Z"} for i in range(5)]
    client = make_fake_client(existing)
    state = {"last_scheduled_day": None, "history": []}
    log, _ = run_daily_replenish(client, CATALOG, state, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log["status"] == "DRY_RUN_SCHEDULED"
    assert log["queue_before"] == 5
    assert log["scheduled_count"] == 1
    assert log["scheduled_items"][0]["due_at_utc"] == "2026-12-06T18:00:00Z"


def test_3_full_queue_and_target_met_do_not_schedule():
    seven = [{"id": f"p{i}", "text": f"Pin {i}", "dueAt": "2026-12-05T18:00:00Z"} for i in range(7)]
    log7, _ = run_daily_replenish(make_fake_client(seven), CATALOG, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log7["status"] == "QUEUE_TARGET_MET" and log7["scheduled_count"] == 0

    ten = [{"id": f"p{i}", "text": f"Pin {i}", "dueAt": "2026-12-05T18:00:00Z"} for i in range(10)]
    log10, _ = run_daily_replenish(make_fake_client(ten), CATALOG, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log10["status"] == "QUEUE_FULL_FREE_LIMIT" and log10["scheduled_count"] == 0


def test_4_duplicate_in_history_or_live_queue_is_skipped():
    first_desc = CATALOG["candidates"][0]["pin_description"]
    live = [{"id": "existing_live", "text": first_desc, "dueAt": "2026-12-03T18:00:00Z"}]
    state = {"last_scheduled_day": None, "history": [{"id": "demo-08-retraso-vuelo-ue261-es", "pin_title": "other", "destination_url": "other"}]}
    log, _ = run_daily_replenish(make_fake_client(live), CATALOG, state, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log["scheduled_count"] == 1
    assert log["scheduled_items"][0]["id"] == "demo-09-madrid-cibeles-retiro-es"
    assert len(log["rejected_candidates"]) == 2


def test_5_api_error_handled_cleanly_and_secret_scrubbed():
    client = make_fake_client([], fail_mode="http_500")
    log, _ = run_daily_replenish(client, CATALOG, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log["status"] == "API_ERROR"
    assert log["scheduled_count"] == 0
    assert "fake_secret_token_123" not in (log["error"] or "")


def test_6_invalid_link_or_missing_anchor_is_rejected():
    def bad_fetcher(url):
        if "esim-viaje-o-roaming" in url:
            return 404, "Not Found"
        return 200, "<html><body>no anchor here</body></html>"

    log, _ = run_daily_replenish(make_fake_client([]), CATALOG, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=bad_fetcher, image_head_fetcher=ok_img_head)
    assert log["status"] == "NO_ELIGIBLE_CANDIDATE"
    assert log["scheduled_count"] == 0
    assert len(log["rejected_candidates"]) == 3


def test_7_missing_or_forbidden_license_is_rejected():
    bad_cat = copy.deepcopy(CATALOG)
    bad_cat["candidates"][0]["media"]["license"] = "CC BY-SA 4.0"
    bad_cat["candidates"][1]["media"]["license"] = ""
    bad_cat["candidates"][2]["media"]["author"] = ""
    log, _ = run_daily_replenish(make_fake_client([]), bad_cat, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log["status"] == "NO_ELIGIBLE_CANDIDATE"
    assert log["scheduled_count"] == 0
    assert len(log["rejected_candidates"]) == 3


def test_8_quota_exhausted_http_429_and_mutation_cap():
    log429, _ = run_daily_replenish(make_fake_client([], fail_mode="quota_429"), CATALOG, {"history": []}, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log429["status"] == "QUOTA_EXHAUSTED"

    log_cap, _ = run_daily_replenish(make_fake_client([], fail_mode="mutation_quota"), CATALOG, {"history": []}, "ch1", dry_run=False, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log_cap["status"] == "QUOTA_EXHAUSTED"


def test_9_workflow_rerun_same_day_is_idempotent():
    client = make_fake_client([])
    state = {"last_scheduled_day": None, "history": []}
    log1, state1 = run_daily_replenish(client, CATALOG, state, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log1["scheduled_count"] == 1
    log2, _ = run_daily_replenish(client, CATALOG, state1, "ch1", dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head)
    assert log2["status"] == "IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY"
    assert log2["scheduled_count"] == 0


def test_10_topic_optimization_blocks_premature_conclusions():
    res = evaluate_topic_optimization([
        {"topic": "madrid", "impressions_pinterest": 42, "clics_sortants_pinterest": 1, "clics_affilies_d1": 0},
        {"topic": "esim", "impressions_pinterest": "N/A", "clics_sortants_pinterest": "N/A"},
        {"topic": "flight", "impressions_pinterest": 1200, "clics_sortants_pinterest": 30, "clics_affilies_d1": 4},
    ])
    assert res[0]["decision"] == "SAMPLE_TOO_SMALL_HOLD"
    assert res[1]["decision"] == "DATA_INCOMPLETE_MANUAL_CHECK_NEEDED"
    assert res[2]["decision"] == "SCALE_TOPIC"


def test_11_live_create_post_rejects_empty_partial_or_invalid_status():
    for mode in ("empty_post", "empty_id", "bad_status"):
        client = make_fake_client([], fail_mode=mode)
        log, st = run_daily_replenish(
            client, CATALOG, {"history": []}, "ch1",
            dry_run=False, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head
        )
        assert log["status"] == "API_ERROR", mode
        assert log["scheduled_count"] == 0, mode
        assert st["history"] == [], mode


def test_12_idempotence_survives_failed_github_commit():
    # Simule : Buffer a créé demo-07, mais le git push GitHub a échoué -> state local encore vide
    cand0 = CATALOG["candidates"][0]
    live_buffer_already_has_it = [
        {"id": "buffer_real_999", "text": cand0["pin_description"], "dueAt": "2026-12-03T18:00:00Z", "status": "scheduled"}
    ]
    empty_state = {"last_scheduled_day": None, "history": []}
    log, _ = run_daily_replenish(
        make_fake_client(live_buffer_already_has_it), CATALOG, empty_state, "ch1",
        dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head
    )
    assert any(r["id"] == cand0["id"] for r in log["rejected_candidates"])
    assert log["scheduled_items"][0]["id"] != cand0["id"]


def test_13_madrid_timezone_cet_cest_and_dst_transitions():
    # Hiver (CET = UTC+1) : 19:00 Madrid = 18:00Z
    winter = compute_next_slot_madrid_utc(
        [{"dueAt": "2026-12-01T18:00:00Z"}],
        now_utc=datetime.datetime(2026, 11, 26, 16, 0, tzinfo=datetime.timezone.utc),
    )
    assert winter == "2026-12-02T18:00:00Z"

    # Été (CEST = UTC+2) : 19:00 Madrid = 17:00Z
    summer = compute_next_slot_madrid_utc(
        [{"dueAt": "2026-07-15T17:00:00Z"}],
        now_utc=datetime.datetime(2026, 7, 10, 12, 0, tzinfo=datetime.timezone.utc),
    )
    assert summer == "2026-07-16T17:00:00Z"

    # Passage heure d'été (dernier dimanche de mars 2027 = 28 mars 2027) : 27/03 en CET -> 28/03 en CEST (17:00Z)
    spring_dst = compute_next_slot_madrid_utc(
        [{"dueAt": "2027-03-27T18:00:00Z"}],
        now_utc=datetime.datetime(2027, 3, 25, 10, 0, tzinfo=datetime.timezone.utc),
    )
    assert spring_dst == "2027-03-28T17:00:00Z"

    # Passage heure d'hiver (dernier dimanche d'octobre 2026 = 25 oct 2026) : 24/10 en CEST -> 25/10 en CET (18:00Z)
    fall_dst = compute_next_slot_madrid_utc(
        [{"dueAt": "2026-10-24T17:00:00Z"}],
        now_utc=datetime.datetime(2026, 10, 20, 10, 0, tzinfo=datetime.timezone.utc),
    )
    assert fall_dst == "2026-10-25T18:00:00Z"


def test_14_image_validation_rejects_404_wrong_mime_or_missing_local_png():
    log_404, _ = run_daily_replenish(
        make_fake_client([]), CATALOG, {"history": []}, "ch1",
        dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=lambda u: (404, "text/plain")
    )
    assert log_404["status"] == "NO_ELIGIBLE_CANDIDATE" and len(log_404["rejected_candidates"]) == 3

    log_mime, _ = run_daily_replenish(
        make_fake_client([]), CATALOG, {"history": []}, "ch1",
        dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=lambda u: (200, "text/html")
    )
    assert log_mime["status"] == "NO_ELIGIBLE_CANDIDATE" and len(log_mime["rejected_candidates"]) == 3

    log_missing_local, _ = run_daily_replenish(
        make_fake_client([]), CATALOG, {"history": []}, "ch1",
        local_pins_dir=os.path.join(ROOT, "nonexistent_dir"),
        dry_run=True, now_utc=NOW, url_fetcher=ok_fetcher, image_head_fetcher=ok_img_head
    )
    assert log_missing_local["status"] == "NO_ELIGIBLE_CANDIDATE" and len(log_missing_local["rejected_candidates"]) == 3


def test_15_catalog_auto_renews_when_below_threshold():
    from pipeline.catalog_renewer import renew_catalog_if_needed

    templates = json.load(open(os.path.join(ROOT, "catalog", "guide_templates.json"), encoding="utf-8"))
    # Simule un catalogue avec 1 seul candidat inutilisé (< threshold=3)
    small_cat = {"candidates": [copy.deepcopy(CATALOG["candidates"][0])]}
    state = {"history": []}
    report, updated = renew_catalog_if_needed(
        catalog=small_cat,
        templates_doc=templates,
        state=state,
        pipeline_root=ROOT,
        font_path=os.path.join(ROOT, "assets", "DejaVuSans-Bold.ttf"),
        out_pins_dir=os.path.join(ROOT, "out", "pins"),
        threshold=3,
        max_add=2,
        url_fetcher=lambda u: (200, '<h2 id="s-dejar-la-maleta-antes-de-un-vuelo-por-la-tarde">ok</h2>'),
        render_images=False,
    )
    assert report["status"] == "RENEWED"
    assert len(report["added_ids"]) == 2
    assert len(updated["candidates"]) == 3


def test_16_business_errors_return_nonzero_exit_code():
    from pipeline.run_autopilot import exit_code_for_status

    for ok_st in ("SCHEDULED_LIVE", "DRY_RUN_SCHEDULED", "QUEUE_TARGET_MET", "QUEUE_FULL_FREE_LIMIT", "IDEMPOTENT_SKIP_ALREADY_SCHEDULED_TODAY"):
        assert exit_code_for_status(ok_st) == 0, ok_st
    for err_st in ("API_ERROR", "QUOTA_EXHAUSTED", "ERROR_BOARD_NOT_FOUND", "NO_ELIGIBLE_CANDIDATE"):
        assert exit_code_for_status(err_st) == 1, err_st
