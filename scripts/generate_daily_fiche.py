#!/usr/bin/env python3
"""
Atlas IUIS 2024 — Générateur quotidien automatisé (GitHub Actions)
- Lit la prochaine IEI en attente dans scripts/queue.json
- Génère la fiche complète (11 blocs + 2 schémas Mermaid) via Gemini API (avec Google Search grounding)
- Indexe automatiquement dans la base Notion "Maladies IUIS 2024"
- Génère automatiquement le quiz hebdomadaire (10 questions : 5 récentes + 5 rétrospectives) tous les 7 jours
- Sauvegarde la fiche Markdown dans fiches/ et prépare fiches/latest_notification.txt
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

QUEUE_PATH = os.path.join(os.path.dirname(__file__), "queue.json")
FICHES_DIR = os.path.join(os.path.dirname(__file__), "..", "fiches")


def http_json(method, url, headers, payload=None, timeout=120):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} on {url}: {body}")


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY manquante.")
    models = ["gemini-flash-latest", "gemini-3-flash-preview", "gemini-3.8-flash", "gemini-3.1-pro-preview"]
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.2},
    }
    last_err = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            res = http_json("POST", url, {"Content-Type": "application/json"}, payload, timeout=180)
            parts = res["candidates"][0]["content"]["parts"]
            print(f"Modèle utilisé : {model}")
            return "".join(p.get("text", "") for p in parts).strip()
        except Exception as e:
            print(f"Échec sur {model} : {e}")
            last_err = e
    raise last_err


# ==================== NOTION HELPERS ====================

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def notion_req(method, path, payload=None):
    return http_json(method, f"https://api.notion.com/v1{path}", notion_headers(), payload, timeout=60)


def parse_rich_text(text):
    if not text:
        return []
    pattern = re.compile(
        r'(\*\*\*[^*]+?\*\*\*|\*\*[^*]+?\*\*|\*[^*\n]+?\*|`[^`\n]+?`|\[[^\]]+?\]\(https?://[^)]+?\))'
    )
    parts = []
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            parts.append((text[pos:m.start()], {}, None))
        tok = m.group(0)
        if tok.startswith("***") and tok.endswith("***"):
            parts.append((tok[3:-3], {"bold": True, "italic": True}, None))
        elif tok.startswith("**") and tok.endswith("**"):
            parts.append((tok[2:-2], {"bold": True}, None))
        elif tok.startswith("*") and tok.endswith("*"):
            parts.append((tok[1:-1], {"italic": True}, None))
        elif tok.startswith("`") and tok.endswith("`"):
            parts.append((tok[1:-1], {"code": True}, None))
        elif tok.startswith("["):
            lm = re.match(r'\[([^\]]+?)\]\((https?://[^)]+?)\)', tok)
            if lm:
                parts.append((lm.group(1), {}, lm.group(2)))
            else:
                parts.append((tok, {}, None))
        pos = m.end()
    if pos < len(text):
        parts.append((text[pos:], {}, None))

    rich = []
    for content, ann, link in parts:
        for i in range(0, len(content), 1900):
            chunk = content[i:i + 1900]
            if not chunk:
                continue
            obj = {"type": "text", "text": {"content": chunk}}
            if link:
                obj["text"]["link"] = {"url": link}
            if ann:
                obj["annotations"] = ann
            rich.append(obj)
    return rich


def md_to_notion_blocks(md_text):
    lines = md_text.splitlines()
    blocks = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        if stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        if stripped.startswith("```"):
            lang = stripped[3:].strip().lower() or "plain text"
            code_lines = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            code_str = "\n".join(code_lines)
            notion_lang = "mermaid" if lang == "mermaid" else "plain text"
            rich_code = [{"type": "text", "text": {"content": code_str[k:k + 1900]}} for k in range(0, len(code_str), 1900)]
            blocks.append({
                "object": "block",
                "type": "code",
                "code": {"rich_text": rich_code or [{"type": "text", "text": {"content": ""}}], "language": notion_lang},
            })
            continue
        if stripped.startswith(">"):
            q_lines = []
            while i < n and lines[i].strip().startswith(">"):
                q_lines.append(lines[i].strip()[1:].strip())
                i += 1
            icon, color = "💡", "blue_background"
            if q_lines and q_lines[0].startswith("[!"):
                tag = q_lines[0].upper()
                q_lines = q_lines[1:]
                if "IMPORTANT" in tag:
                    icon, color = "📘", "purple_background"
                elif "TIP" in tag:
                    icon, color = "⚡", "blue_background"
                elif "WARNING" in tag or "CAUTION" in tag:
                    icon, color = "⚠️", "yellow_background"
            blocks.append({
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": parse_rich_text(" ".join(l for l in q_lines if l)),
                    "icon": {"type": "emoji", "emoji": icon},
                    "color": color,
                },
            })
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            tbl_lines = []
            while i < n and lines[i].strip().startswith("|"):
                tbl_lines.append(lines[i].strip())
                i += 1
            rows = []
            for tl in tbl_lines:
                cells = [c.strip() for c in tl.strip("|").split("|")]
                if all(re.match(r'^:?-+:?$', c) for c in cells if c != ""):
                    continue
                rows.append(cells)
            if rows:
                width = max(len(r) for r in rows)
                t_rows = []
                for r in rows:
                    padded = r + [""] * (width - len(r))
                    t_rows.append({
                        "object": "block",
                        "type": "table_row",
                        "table_row": {"cells": [parse_rich_text(c) for c in padded]},
                    })
                blocks.append({
                    "object": "block",
                    "type": "table",
                    "table": {"table_width": width, "has_column_header": True, "has_row_header": False, "children": t_rows},
                })
            continue
        if stripped.startswith("### "):
            blocks.append({"object": "block", "type": "heading_3", "heading_3": {"rich_text": parse_rich_text(stripped[4:].strip())}})
            i += 1
            continue
        if stripped.startswith("## "):
            blocks.append({"object": "block", "type": "heading_2", "heading_2": {"rich_text": parse_rich_text(stripped[3:].strip())}})
            i += 1
            continue
        if stripped.startswith("# "):
            blocks.append({"object": "block", "type": "heading_1", "heading_1": {"rich_text": parse_rich_text(stripped[2:].strip())}})
            i += 1
            continue
        if re.match(r'^[-*] ', stripped):
            blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": parse_rich_text(stripped[2:].strip())}})
            i += 1
            continue
        nm = re.match(r'^\d+\.\s+(.*)$', stripped)
        if nm:
            blocks.append({"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": parse_rich_text(nm.group(1).strip())}})
            i += 1
            continue
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": parse_rich_text(stripped)}})
        i += 1
    return blocks


def find_notion_disease_page(db_id, search_term):
    res = notion_req("POST", f"/databases/{db_id}/query", {
        "filter": {"or": [
            {"property": "Gene", "rich_text": {"contains": search_term}},
            {"property": "Disease", "title": {"contains": search_term}},
        ]},
        "page_size": 5,
    })
    results = res.get("results", [])
    return results[0] if results else None


def append_blocks_to_notion(page_id, blocks):
    for idx in range(0, len(blocks), 90):
        notion_req("PATCH", f"/blocks/{page_id}/children", {"children": blocks[idx:idx + 90]})


# ==================== MAIN PIPELINE ====================

def build_fiche_prompt(item):
    return f"""Tu es un immunologiste clinicien expert rédigeant la Fiche #{item['num']} ({item['day']}, Semaine {item['week']}) du protocole **Atlas IUIS 2024**.

Cible : **{item['name']}** (Gène : *{item['gene']}*, Table IUIS 2024 : **{item['table']}**).

Utilise Google Search pour vérifier les données des registres (ESID, USIDNET, CEREDIH, PIDTC), les recommandations récentes et 4 à 5 publications majeures récentes (2022–2026) avec des **PMID ou PMCID exacts et vérifiés** (n'invente JAMAIS un PMID/PMCID/DOI).

Rédige en **français médical rigoureux** la fiche complète au format Markdown avec **exactement les 11 blocs et 2 schémas Mermaid** :
- `# Fiche #{item['num']} — {item['name']}`
- Callout `> [!IMPORTANT]` avec Semaine {item['week']}, {item['day']}
- `## 0. Carte d'identité IUIS 2024` (tableau Markdown)
- `## 1. TL;DR (Synthèse exécutive)` (callout `> [!TIP]` de 6–8 lignes)
- `## 2. Contexte historique`
- `## 3. Épidémiologie & Données de registres`
- `## 4. Physiopathologie, Génétique moléculaire & Schéma 1` (inclure un bloc ` ```mermaid ` `flowchart TD` détaillé)
- `## 5. Présentation clinique, Pathogènes & Phénotype immunologique`
- `## 6. Complications à long terme & Défis non résolus`
- `## 7. Avenues thérapeutiques`
- `## 8. Section pratique (Bedside, Guidelines & Schéma 2)` (inclure ` ```mermaid ` algorithme diagnostique/prise en charge, tableau Markdown de diagnostic différentiel, et 6 perles cliniques numérotées)
- `## 9. Nouveautés & Littérature récente (2022–2026)`
- `## 10. Références vérifiées`

Ne mets aucun commentaire avant ou après le Markdown."""


def build_quiz_prompt(week_items, past_items):
    recent_str = ", ".join(f"{x['day']} {x['name']} ({x['gene']})" for x in week_items)
    past_str = ", ".join(f"{x['day']} {x['name']} ({x['gene']})" for x in past_items) if past_items else "XLA (BTK), DICV (CVID)"
    return f"""Tu es un immunologiste clinicien rédigeant le **Quiz hebdomadaire — Semaine {week_items[0]['week']}** du protocole Atlas IUIS 2024.

Génère exactement **10 questions QCM** (vignettes cliniques, pièges diagnostiques, mécanismes moléculaires, conduites à tenir) :
- **5 questions** sur les IEI de cette semaine : {recent_str}
- **5 questions rétrospectives** (répétition espacée) sur : {past_str}

Format strict pour chaque question :
### Question N — [Sujet]
Énoncé de la vignette clinique...
- A) ...
- B) ...
- C) ...
- D) ...
**Réponse correcte : X** — Explication détaillée en 3–4 lignes."""


def main():
    with open(QUEUE_PATH, "r", encoding="utf-8") as f:
        queue = json.load(f)

    pending = [it for it in queue["items"] if it["status"] == "pending"]
    if not pending:
        print("Toutes les fiches de queue.json sont déjà terminées.")
        return

    item = pending[0]
    print(f"Lancement Fiche #{item['num']} ({item['day']}) : {item['name']}")

    fiche_md = call_gemini(build_fiche_prompt(item))
    os.makedirs(FICHES_DIR, exist_ok=True)
    slug = re.sub(r'[^a-z0-9]+', '_', item['gene'].lower()).strip('_')
    md_filename = f"fiche_{item['num']}_{slug}.md"
    md_path = os.path.join(FICHES_DIR, md_filename)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(fiche_md)
    print(f"Markdown sauvegardé : {md_path}")

    notion_url = "(dry-run)"
    if not DRY_RUN:
        if not NOTION_TOKEN:
            raise RuntimeError("NOTION_TOKEN manquant.")
        page = find_notion_disease_page(queue["notion_database_id"], item["notion_search"])
        if page:
            page_id = page["id"]
            notion_url = page.get("url", "")
            notion_req("PATCH", f"/pages/{page_id}", {
                "properties": {"Statut": {"select": {"name": "📝 Notes disponibles"}}}
            })
            blocks = [{"object": "block", "type": "divider", "divider": {}}] + md_to_notion_blocks(fiche_md)
            append_blocks_to_notion(page_id, blocks)
            print(f"Indexé dans Notion : {notion_url}")
        else:
            print(f"ATTENTION: Page Notion non trouvée pour '{item['notion_search']}'.")

    item["status"] = "done"
    item["completed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Vérifier si la semaine de cet item est complète (7 items done dans cette semaine)
    week_items = [x for x in queue["items"] if x["week"] == item["week"]]
    if all(x["status"] == "done" for x in week_items):
        past_items = [x for x in queue["items"] if x["week"] < item["week"] and x["status"] == "done"]
        quiz_md = call_gemini(build_quiz_prompt(week_items, past_items))
        quiz_path = os.path.join(FICHES_DIR, f"quiz_semaine_{item['week']}.md")
        with open(quiz_path, "w", encoding="utf-8") as f:
            f.write(quiz_md)
        if not DRY_RUN and queue.get("notion_quiz_page_id"):
            q_blocks = [{"object": "block", "type": "divider", "divider": {}}] + md_to_notion_blocks(quiz_md)
            append_blocks_to_notion(queue["notion_quiz_page_id"], q_blocks)
            print(f"Quiz Semaine {item['week']} ajouté dans Notion.")

    with open(QUEUE_PATH, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)

    notif_path = os.path.join(FICHES_DIR, "latest_notification.txt")
    with open(notif_path, "w", encoding="utf-8") as f:
        f.write(
            f"Nouvelle fiche Atlas IUIS 2024 disponible !\n\n"
            f"Fiche #{item['num']} ({item['day']}, Semaine {item['week']}) : {item['name']} ({item['gene']})\n"
            f"Table IUIS : {item['table']}\n"
            f"Lien Notion : {notion_url}\n"
        )
    print("Terminé avec succès.")


if __name__ == "__main__":
    main()
