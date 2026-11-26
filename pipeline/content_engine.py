"""
Moteur de contenu Pinterest ECOM-ZERO (extensible ES / FR / EN).
- Priorise les guides avec offres affiliées APPROVED actives.
- Vérifie les licences (allowlist stricte : Pexels License, CC0, Public domain, CC BY 2.0/3.0/4.0).
- Vérifie les URL de destination HTTP 200 et la présence des ancres HTML (#id).
- Détecte les doublons textuels (titre/URL normalisés) et visuels (SHA-256).
- Génère des visuels verticaux 1000x1500 px avec mention commerciale transparente.
"""
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from PIL import Image, ImageDraw, ImageFont

W, H = 1000, 1500
ALLOWED_LICENSES = {"Pexels License", "CC0", "Public domain", "CC BY 2.0", "CC BY 3.0", "CC BY 4.0"}


def normalize_text(s):
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    return s


def build_utm_url(base_url, utm_campaign, anchor=None):
    sep = "&" if "?" in base_url else "?"
    url = f"{base_url}{sep}utm_source=pinterest&utm_medium=social&utm_campaign={urllib.parse.quote(utm_campaign)}"
    if anchor:
        url += f"#{anchor.lstrip('#')}"
    return url


def validate_license(media_meta):
    lic = (media_meta or {}).get("license", "").strip()
    src = (media_meta or {}).get("source_url", "").strip()
    author = (media_meta or {}).get("author", "").strip()
    if not lic or lic not in ALLOWED_LICENSES:
        return False, f"Licence non autorisée ou manquante: '{lic}'"
    if not src.startswith("https://") or not author:
        return False, "Source URL HTTPS ou auteur manquant dans les métadonnées média."
    return True, "OK"


def validate_destination_url(url, anchor=None, fetcher=None):
    """Vérifie que l'URL de destination répond HTTP 200 et contient l'ancre HTML #id si spécifiée."""
    clean_url = url.split("#")[0]
    if not clean_url.startswith("https://ecom-zero.pages.dev/"):
        return False, f"Domaine non autorisé: {clean_url}"
    try:
        if fetcher is not None:
            status, html = fetcher(clean_url)
        else:
            req = urllib.request.Request(clean_url, headers={"User-Agent": "ecomzero-pinterest-check/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                status = r.status
                html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} sur {clean_url}"
    except Exception as e:
        return False, f"Erreur réseau sur {clean_url}: {type(e).__name__}"

    if status != 200:
        return False, f"Statut HTTP invalide ({status}) sur {clean_url}"
    if anchor:
        clean_anchor = anchor.lstrip("#")
        if f'id="{clean_anchor}"' not in html:
            return False, f"Ancre HTML #{clean_anchor} introuvable sur {clean_url}"
    return True, "OK"


def validate_image_asset(image_url, local_png_path=None, head_fetcher=None):
    """
    Vérifie avant toute mutation Buffer :
    - URL HTTPS publique directe (sans redirection) répondant HTTP 200 avec Content-Type image/png.
    - Si local_png_path est fourni : existence du fichier PNG et ratio vertical compatible Pinterest.
    """
    if not image_url or not image_url.startswith("https://"):
        return False, f"URL image non HTTPS: '{image_url}'"

    if local_png_path is not None:
        if not os.path.exists(local_png_path):
            return False, f"Fichier PNG local introuvable: {local_png_path}"
        try:
            with Image.open(local_png_path) as im:
                w, h = im.size
                ratio = w / h if h else 0
                if im.format != "PNG" or w < 600 or h < 900 or not (0.5 <= ratio <= 0.85):
                    return False, f"Format image incompatible Pinterest: {im.format} {w}x{h}"
        except Exception as e:
            return False, f"Image PNG locale illisible: {type(e).__name__}"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        if head_fetcher is not None:
            status, content_type = head_fetcher(image_url)
        else:
            req = urllib.request.Request(image_url, method="HEAD", headers={"User-Agent": "ecomzero-pinterest-check/1.0"})
            opener = urllib.request.build_opener(_NoRedirect)
            with opener.open(req, timeout=15) as r:
                status = r.status
                content_type = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return False, f"Image HTTPS inaccessible (HTTP {e.code}) sur {image_url}"
    except Exception as e:
        return False, f"Erreur réseau image sur {image_url}: {type(e).__name__}"

    if status != 200:
        return False, f"Statut HTTP image invalide ({status}) sur {image_url}"
    if not str(content_type).lower().startswith("image/png"):
        return False, f"Content-Type image invalide ('{content_type}') sur {image_url}"
    return True, "OK"


def is_duplicate(candidate, history_entries, scheduled_posts):
    cand_title = normalize_text(candidate.get("pin_title"))
    cand_desc = normalize_text(candidate.get("pin_description"))
    cand_url = build_utm_url(candidate["destination_url"], candidate["utm_campaign"], candidate.get("anchor"))

    for h in history_entries:
        if h.get("id") == candidate.get("id"):
            return True, f"ID déjà présent dans l'historique: {candidate.get('id')}"
        if normalize_text(h.get("pin_title")) == cand_title:
            return True, f"Titre déjà publié/programmé: {candidate.get('pin_title')}"
        if h.get("destination_url") == cand_url:
            return True, f"URL UTM déjà utilisée: {cand_url}"

    for p in scheduled_posts:
        pt = normalize_text(p.get("text", ""))
        if cand_desc and cand_desc[:80] in pt:
            return True, f"Description déjà présente dans Buffer (post {p.get('id', '?')})."
        if cand_title and cand_title in pt:
            return True, f"Titre déjà présent dans Buffer (post {p.get('id', '?')})."
    return False, "OK"


def validate_disclosure(candidate):
    desc = normalize_text(candidate.get("pin_description"))
    foot = normalize_text(candidate.get("media", {}).get("attribution_footer", ""))
    has_aff_mention = any(k in desc for k in ("afiliado", "affiliation", "affiliate")) and any(
        k in foot for k in ("afiliado", "affiliation", "affiliate")
    )
    if candidate.get("affiliates_approved") and not has_aff_mention:
        return False, "Mention commerciale d'affiliation manquante sur la description ou le visuel."
    return True, "OK"


def wrap_text(draw, text, font, max_w):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_pin_png(candidate, repo_root, font_path, out_png_path):
    media = candidate["media"]
    src_path = os.path.join(repo_root, media["local_path"])
    if media["type"] == "video_frame":
        tmp_jpg = out_png_path + ".frame.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", str(media.get("frame_sec", "2.0")), "-i", src_path, "-frames:v", "1", tmp_jpg],
            check=True,
        )
        src = Image.open(tmp_jpg).convert("RGB")
        try:
            os.remove(tmp_jpg)
        except OSError:
            pass
    else:
        src = Image.open(src_path).convert("RGB")

    canvas = Image.new("RGB", (W, H), (18, 21, 28))
    hero_h = 820
    scale = max(W / src.width, hero_h / src.height)
    resized = src.resize((int(src.width * scale) + 1, int(src.height * scale) + 1), Image.LANCZOS)
    x0 = (resized.width - W) // 2
    y0 = (resized.height - hero_h) // 2
    canvas.paste(resized.crop((x0, y0, x0 + W, y0 + hero_h)), (0, 0))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    for y in range(0, 170):
        od.line([(0, y), (W, y)], fill=(12, 15, 22, int(130 * (1 - y / 170))))
    for y in range(hero_h - 240, hero_h):
        od.line([(0, y), (W, y)], fill=(18, 21, 28, int(255 * ((y - (hero_h - 240)) / 240))))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")

    d = ImageDraw.Draw(canvas)
    accent = tuple(candidate.get("accent_rgb", [255, 208, 20]))
    f_badge = ImageFont.truetype(font_path, 26)
    bw = int(d.textlength(candidate["badge"], font=f_badge)) + 48
    d.rounded_rectangle((56, 48, 56 + bw, 104), 28, fill=accent)
    d.text((80, 60), candidate["badge"], font=f_badge, fill=(18, 20, 26))

    f_ez = ImageFont.truetype(font_path, 24)
    ez_w = int(d.textlength("ECOM-ZERO", font=f_ez)) + 40
    d.rounded_rectangle((W - 56 - ez_w, 48, W - 56, 104), 28, fill=(18, 21, 28))
    d.text((W - 56 - ez_w + 20, 61), "ECOM-ZERO", font=f_ez, fill=(255, 255, 255))

    f_title = ImageFont.truetype(font_path, 54)
    ty = 655
    for line in wrap_text(d, candidate["title_visual"], f_title, W - 112):
        d.text((56, ty), line, font=f_title, fill=(255, 255, 255), stroke_width=3, stroke_fill=(12, 14, 20))
        ty += 66

    div_y = ty + 16
    d.rounded_rectangle((56, div_y, 196, div_y + 8), 4, fill=accent)

    f_bul = ImageFont.truetype(font_path, 29)
    by = div_y + 34
    for b in candidate["bullets"]:
        d.ellipse((60, by + 10, 76, by + 26), fill=accent)
        for bl in wrap_text(d, b, f_bul, W - 160):
            d.text((96, by), bl, font=f_bul, fill=(235, 240, 246))
            by += 39
        by += 14

    f_cta = ImageFont.truetype(font_path, 30)
    d.rounded_rectangle((56, 1315, W - 56, 1395), 40, fill=accent)
    cw = d.textlength(candidate["cta"], font=f_cta)
    d.text(((W - cw) / 2, 1338), candidate["cta"], font=f_cta, fill=(18, 20, 26))

    f_foot = ImageFont.truetype(font_path, 20)
    d.text((56, 1435), media["attribution_footer"], font=f_foot, fill=(175, 184, 198))

    os.makedirs(os.path.dirname(out_png_path), exist_ok=True)
    canvas.save(out_png_path, "PNG", optimize=True)
    with open(out_png_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()
    return {"path": out_png_path, "width": W, "height": H, "sha256": sha256}
