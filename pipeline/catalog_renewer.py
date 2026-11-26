"""
Renouvellement autonome du catalogue Pinterest à partir des guides ECOM-ZERO et des offres APPROVED.
- Vérifie le nombre de candidats non encore utilisés dans catalog/guides_catalog.json.
- Quand la réserve descend sous RENEWAL_THRESHOLD (3), puise dans catalog/guide_templates.json,
  valide licence + divulgation + lien HTTP 200 + ancre HTML + absence de doublon,
  génère le PNG 1000x1500 dans out/pins/<id>.png, et l'ajoute au catalogue.
- N'invente jamais de prix, de promotion ni de tendance non vérifiée.
"""
import json
import os

from .content_engine import (
    build_utm_url,
    is_duplicate,
    render_pin_png,
    validate_destination_url,
    validate_disclosure,
    validate_license,
)

RENEWAL_THRESHOLD = 3
MAX_RENEW_PER_RUN = 2


def count_unused_candidates(catalog, history_entries):
    used_ids = {h.get("id") for h in history_entries}
    return [c for c in catalog.get("candidates", []) if c.get("id") not in used_ids]


def renew_catalog_if_needed(
    catalog,
    templates_doc,
    state,
    pipeline_root,
    font_path,
    out_pins_dir,
    threshold=RENEWAL_THRESHOLD,
    max_add=MAX_RENEW_PER_RUN,
    url_fetcher=None,
    render_images=True,
):
    history = state.get("history", [])
    unused = count_unused_candidates(catalog, history)
    report = {
        "unused_before": len(unused),
        "threshold": threshold,
        "added_ids": [],
        "skipped": [],
    }
    if len(unused) >= threshold:
        report["status"] = "CATALOG_SUFFICIENT"
        return report, catalog

    existing_ids = {c.get("id") for c in catalog.get("candidates", [])}
    added = 0
    for tpl in templates_doc.get("templates", []):
        if added >= max_add:
            break
        if tpl["id"] in existing_ids:
            continue

        lic_ok, lic_msg = validate_license(tpl.get("media"))
        if not lic_ok:
            report["skipped"].append({"id": tpl["id"], "reason": lic_msg})
            continue

        disc_ok, disc_msg = validate_disclosure(tpl)
        if not disc_ok:
            report["skipped"].append({"id": tpl["id"], "reason": disc_msg})
            continue

        dup, dup_msg = is_duplicate(tpl, history + catalog.get("candidates", []), [])
        if dup:
            report["skipped"].append({"id": tpl["id"], "reason": dup_msg})
            continue

        dest = build_utm_url(tpl["destination_url"], tpl["utm_campaign"], tpl.get("anchor"))
        url_ok, url_msg = validate_destination_url(dest, anchor=tpl.get("anchor"), fetcher=url_fetcher)
        if not url_ok:
            report["skipped"].append({"id": tpl["id"], "reason": url_msg})
            continue

        if render_images:
            out_png = os.path.join(out_pins_dir, f"{tpl['id']}.png")
            if not os.path.exists(out_png):
                render_pin_png(tpl, pipeline_root, font_path, out_png)

        catalog.setdefault("candidates", []).append(tpl)
        existing_ids.add(tpl["id"])
        report["added_ids"].append(tpl["id"])
        added += 1

    report["status"] = "RENEWED" if added > 0 else "NO_NEW_TEMPLATE_AVAILABLE"
    return report, catalog
