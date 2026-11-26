"""
Suivi commercial Pinterest & optimisation de sujets avec garde-fou anti-surinterprétation.
Distingue strictement :
- Automatisable gratuitement par API : visites D1 (GET /api/telemetry?all=true), clics affiliés D1, statuts Buffer.
- Relevé manuel requis (hors API gratuite) : impressions Pinterest, clics sortants Pinterest, réservations/commissions Travelpayouts & Trip Tours.
"""
import json
import urllib.request

TELEMETRY_URL = "https://ecom-zero.pages.dev/api/telemetry?all=true"
MIN_IMPRESSIONS_FOR_DECISION = 500
MIN_OUTBOUND_CLICKS_FOR_DECISION = 20


def fetch_d1_telemetry(url=TELEMETRY_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "ecomzero-pinterest-metrics/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    verified = (data.get("attribution") or {}).get("verified") or []
    pinterest_events = [
        ev for ev in verified
        if ev.get("ref_class") == "pinterest" or ev.get("utm_source") == "pinterest"
    ]
    return {
        "fetched_date": data.get("metrics", {}).get("date"),
        "total_affiliate_clicks_d1": data.get("metrics", {}).get("affiliateClicks", 0),
        "pinterest_attributed_events": pinterest_events,
        "affiliate_clicks_by_guide": data.get("affiliateClicksByGuide"),
    }


def evaluate_topic_optimization(topic_rows):
    """
    Classe les sujets selon CTR sortant et clics affiliés, MAIS refuse toute conclusion
    si l'échantillon est trop faible (< 500 impressions ou < 20 clics sortants).
    """
    decisions = []
    for row in topic_rows:
        imp = row.get("impressions_pinterest")
        clicks = row.get("clics_sortants_pinterest")
        aff = row.get("clics_affilies_d1", 0)
        if not isinstance(imp, (int, float)) or not isinstance(clicks, (int, float)):
            decisions.append({"topic": row["topic"], "decision": "DATA_INCOMPLETE_MANUAL_CHECK_NEEDED"})
            continue
        if imp < MIN_IMPRESSIONS_FOR_DECISION or clicks < MIN_OUTBOUND_CLICKS_FOR_DECISION:
            decisions.append({
                "topic": row["topic"],
                "decision": "SAMPLE_TOO_SMALL_HOLD",
                "detail": f"Échantillon insuffisant ({imp} imp., {clicks} clics < seuils {MIN_IMPRESSIONS_FOR_DECISION}/{MIN_OUTBOUND_CLICKS_FOR_DECISION}).",
            })
            continue
        ctr = round(clicks / imp * 100, 2)
        decisions.append({
            "topic": row["topic"],
            "ctr_pct": ctr,
            "affiliate_clicks": aff,
            "decision": "SCALE_TOPIC" if (ctr >= 1.5 and aff > 0) else "ITERATE_CREATIVE",
        })
    return decisions
