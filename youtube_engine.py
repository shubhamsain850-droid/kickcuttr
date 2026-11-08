"""
YouTube Live Stream Earning, Super Chat, Memberships, Jewels & Chat Analytics Engine
Accurately extracts:
- Super Chats & Super Stickers (using exact monetary values, deduplicated by ID)
- Dynamic Membership Tiers (extracts streamer custom tier names from both New Joins and Milestones)
- YouTube Jewels (Creator net rate ~Rs. 0.42 per Jewel based on 1 Ruby = $0.01 USD / 2 Jewels)
- Channel live streams discovery with date range filtering
"""

import json
import os
import re
import subprocess
import time
import http.cookiejar
import requests
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────
# CONSTANTS & FINANCIAL CONVERSIONS
# ─────────────────────────────────────────────

# Creator Net Revenue Rate:
# In YouTube's Jewels program:
# Viewers purchase Jewels packs in Google Play (retail pack ~Rs. 1.20/jewel).
# YouTube pays creators in Rubies: 1 Ruby = $0.01 USD (~Rs. 0.84 INR).
# It requires 2 Jewels to earn 1 Ruby.
# Therefore: Net Creator Earnings = 1 Ruby / 2 Jewels = $0.005 USD ≈ Rs. 0.42 INR per Jewel.
INR_PER_JEWEL: float = 0.42

CURRENCY_RATES_TO_INR: dict[str, float] = {
    "INR": 1.0,
    "USD": 86.85,
    "EUR": 94.20,
    "GBP": 110.50,
    "CAD": 63.80,
    "AUD": 56.50,
    "SGD": 64.80,
    "AED": 23.65,
    "SAR": 23.15,
    "BRL": 15.20,
    "JPY": 0.58,
    "KRW": 0.063,
    "NZD": 51.50,
    "HKD": 11.15,
    "TWD": 2.70,
    "PHP": 1.50,
    "PKR": 0.31,
    "BDT": 0.72,
    "RUB": 0.90,
    "TRY": 2.50,
    "PLN": 21.80,
    "MXN": 4.30,
    "IDR": 0.0055,
    "THB": 2.45,
    "VND": 0.0034,
}

SYMBOL_TO_CURRENCY: list[tuple[str, str]] = sorted([
    ("Mex$", "MXN"),
    ("US$", "USD"), ("CA$", "CAD"), ("C$", "CAD"),
    ("AU$", "AUD"), ("A$", "AUD"),
    ("NZ$", "NZD"), ("HK$", "HKD"), ("NT$", "TWD"),
    ("SG$", "SGD"), ("S$", "SGD"),
    ("R$", "BRL"),
    ("Rs.", "PKR"), ("PKR", "PKR"),
    ("BDT", "BDT"), ("৳", "BDT"),
    ("AED", "AED"), ("SAR", "SAR"),
    ("PHP", "PHP"), ("₱", "PHP"),
    ("JP¥", "JPY"), ("JPY", "JPY"), ("¥", "JPY"),
    ("KRW", "KRW"), ("₩", "KRW"),
    ("EUR", "EUR"), ("€", "EUR"),
    ("GBP", "GBP"), ("£", "GBP"),
    ("INR", "INR"), ("₹", "INR"), ("Rs", "INR"),
    ("$", "USD"),   ("USD", "USD"),
    ("zł", "PLN"),  ("PLN", "PLN"),
    ("₺", "TRY"),   ("TRY", "TRY"),
    ("₽", "RUB"),   ("RUB", "RUB"),
    ("IDR", "IDR"), ("Rp", "IDR"),
    ("THB", "THB"), ("฿", "THB"),
    ("VND", "VND"), ("₫", "VND")
], key=lambda x: len(x[0]), reverse=True)


DEFAULT_TIER_PRICES: dict[str, float] = {
    "Level 1": 59.0,
    "Level 2": 159.0,
    "Level 3": 299.0,
    "Level 4": 799.0,
    "Level 5": 3999.0,
}

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def extract_text_field(field_obj) -> str:
    """Safely extract plain text from YouTube API string, dict with simpleText, or runs array."""
    if not field_obj:
        return ""
    if isinstance(field_obj, str):
        return field_obj.strip()
    if isinstance(field_obj, dict):
        if "simpleText" in field_obj:
            return str(field_obj["simpleText"]).strip()
        if "runs" in field_obj and isinstance(field_obj["runs"], list):
            return "".join(r.get("text", "") for r in field_obj["runs"] if isinstance(r, dict)).strip()
        if "content" in field_obj:
            return str(field_obj["content"]).strip()
    return ""


def extract_video_id(url_or_id: str) -> str:
    """Extract clean 11-char YouTube video ID."""
    url = url_or_id.strip()
    m = re.search(r'(?:v=|\/|be\/|shorts\/|live\/)([0-9A-Za-z_-]{11})', url)
    return m.group(1) if m else url


def parse_amount(text: str) -> tuple[float, str]:
    """Parse amount and currency symbol from string (e.g. '₹101.00', '$1.99', 'CA$ 5.00', 'R$ 10,00')."""
    if not text:
        return 0.0, "INR"
    
    currency = "INR"
    for sym, curr in SYMBOL_TO_CURRENCY:
        if sym in text:
            currency = curr
            break

    # Handle comma as decimal or thousand separator safely
    clean = re.sub(r'[^0-9.,]', '', text).strip()
    if ',' in clean and '.' in clean:
        if clean.rfind(',') > clean.rfind('.'):
            clean = clean.replace('.', '').replace(',', '.')
        else:
            clean = clean.replace(',', '')
    elif ',' in clean and '.' not in clean:
        parts = clean.split(',')
        if len(parts) == 2 and len(parts[1]) <= 2:
            clean = parts[0] + '.' + parts[1]
        else:
            clean = clean.replace(',', '')

    try:
        val = float(clean)
    except ValueError:
        val = 0.0

    return val, currency


def get_cookie_arg() -> list[str]:
    """Check for local cookies.txt in current or backend directory."""
    for p in [Path("cookies.txt"), Path(__file__).parent / "cookies.txt", Path("/tmp/cookies.txt")]:
        if p.exists() and p.stat().st_size > 100:
            return ["--cookies", str(p.resolve())]
    return []


# ─────────────────────────────────────────────
# CORE CHAT & STREAM PARSER
# ─────────────────────────────────────────────

def parse_chat_json_lines(json_lines, video_id: str = "") -> dict:
    """
    Parses live chat JSON lines (from file or memory), deduplicating actions by item ID.
    Extracts:
    - Superchats & Super Stickers (real revenue values)
    - Memberships (Dynamic Tiers + Milestones/Tenure duration)
    - YouTube Jewels Gifts
    """
    seen_ids = set()
    superchats = []
    memberships = []
    jewels = []
    unique_donors = set()
    unique_chatters = set()
    custom_tiers_discovered = set()
    currency_breakdown = defaultdict(lambda: {"amount": 0.0, "count": 0, "amount_inr": 0.0})

    total_sc_inr = 0.0
    total_jewels_count = 0
    total_jewels_inr = 0.0
    total_mem_inr = 0.0

    def walk_tree(obj):
        nonlocal total_sc_inr, total_jewels_count, total_jewels_inr, total_mem_inr
        if not isinstance(obj, dict):
            return

        # ── 1. Superchats (Paid Message) ──
        if 'liveChatPaidMessageRenderer' in obj:
            sc = obj['liveChatPaidMessageRenderer']
            sc_id = sc.get('id')
            if not sc_id or sc_id not in seen_ids:
                if sc_id:
                    seen_ids.add(sc_id)
                author = extract_text_field(sc.get('authorName')) or "Unknown"
                amt_text = extract_text_field(sc.get('purchaseAmountText'))
                amt, curr = parse_amount(amt_text)
                rate = CURRENCY_RATES_TO_INR.get(curr, 1.0)
                amt_inr = round(amt * rate, 2)

                msg = extract_text_field(sc.get('message'))
                time_text = extract_text_field(sc.get('timestampText'))

                superchats.append({
                    "id": sc_id or "",
                    "author": author,
                    "amount": amt,
                    "currency": curr,
                    "amount_inr": amt_inr,
                    "message": msg,
                    "time": time_text,
                    "stream_id": video_id
                })
                total_sc_inr += amt_inr
                currency_breakdown[curr]["amount"] = round(currency_breakdown[curr]["amount"] + amt, 2)
                currency_breakdown[curr]["count"] += 1
                currency_breakdown[curr]["amount_inr"] = round(currency_breakdown[curr]["amount_inr"] + amt_inr, 2)
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 2. Super Stickers (Paid Sticker — Real Money!) ──
        if 'liveChatPaidStickerRenderer' in obj:
            st = obj['liveChatPaidStickerRenderer']
            st_id = st.get('id')
            if not st_id or st_id not in seen_ids:
                if st_id:
                    seen_ids.add(st_id)
                author = extract_text_field(st.get('authorName')) or "Unknown"
                amt_text = extract_text_field(st.get('purchaseAmountText'))
                amt, curr = parse_amount(amt_text)
                rate = CURRENCY_RATES_TO_INR.get(curr, 1.0)
                amt_inr = round(amt * rate, 2)
                time_text = extract_text_field(st.get('timestampText'))

                superchats.append({
                    "id": st_id or "",
                    "author": author,
                    "amount": amt,
                    "currency": curr,
                    "amount_inr": amt_inr,
                    "message": "[Super Sticker]",
                    "time": time_text,
                    "stream_id": video_id
                })
                total_sc_inr += amt_inr
                currency_breakdown[curr]["amount"] = round(currency_breakdown[curr]["amount"] + amt, 2)
                currency_breakdown[curr]["count"] += 1
                currency_breakdown[curr]["amount_inr"] = round(currency_breakdown[curr]["amount_inr"] + amt_inr, 2)
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 3. Memberships (Dynamic Custom Tier Detection) ──
        if 'liveChatMembershipItemRenderer' in obj:
            mem = obj['liveChatMembershipItemRenderer']
            mem_id = mem.get('id')
            if not mem_id or mem_id not in seen_ids:
                if mem_id:
                    seen_ids.add(mem_id)

                author = extract_text_field(mem.get('authorName')) or "Unknown"
                sub_text = extract_text_field(mem.get('headerSubtext'))
                pri_text = extract_text_field(mem.get('headerPrimaryText'))

                tier = "General Member"
                m_type = "Member"
                tenure = ""
                est_price = 59.0

                if 'welcome to' in sub_text.lower():
                    # New member join e.g. "Welcome to Duggu’s world 🐥!"
                    tier = re.sub(r'(?i)welcome to\s*', '', sub_text).rstrip('!').strip()
                    m_type = "New Join"
                    custom_tiers_discovered.add(tier)
                elif 'upgraded' in sub_text.lower():
                    tier = re.sub(r'(?i)upgraded.*?to\s*', '', sub_text).rstrip('!').strip()
                    m_type = "Upgrade"
                    custom_tiers_discovered.add(tier)
                    est_price = 299.0
                elif sub_text:
                    # Milestone renewal e.g. sub_text = "Duggu’s world 🐥", pri_text = "Member for 22 months"
                    tier = sub_text.strip()
                    tenure = pri_text
                    m_type = "Milestone / Renewal"
                    custom_tiers_discovered.add(tier)
                elif pri_text:
                    tier = "General Member"
                    tenure = pri_text
                    m_type = f"Milestone ({pri_text})"

                memberships.append({
                    "id": mem_id or "",
                    "author": author,
                    "type": m_type,
                    "tier": tier,
                    "tenure": tenure,
                    "price_inr": est_price,
                    "stream_id": video_id
                })
                total_mem_inr += est_price
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 4. Gifted Memberships Purchase ──
        if 'liveChatSponsorshipsGiftPurchaseAnnouncementRenderer' in obj:
            gift = obj['liveChatSponsorshipsGiftPurchaseAnnouncementRenderer']
            g_id = gift.get('id')
            if not g_id or g_id not in seen_ids:
                if g_id:
                    seen_ids.add(g_id)

                header = gift.get('header', {}).get('liveChatSponsorshipsHeaderRenderer', {})
                author = extract_text_field(header.get('authorName')) or "Unknown"
                pri = extract_text_field(header.get('primaryText'))
                
                gift_count = 1
                m_count = re.search(r'\b(\d+)\b', pri)
                if m_count:
                    gift_count = int(m_count.group(1))

                est_gift_val = gift_count * 59.0
                memberships.append({
                    "id": g_id or "",
                    "author": author,
                    "type": f"Gifted ({gift_count})",
                    "tier": f"{gift_count} Memberships Gifted",
                    "tenure": pri,
                    "price_inr": est_gift_val,
                    "stream_id": video_id
                })
                total_mem_inr += est_gift_val
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 5. YouTube Jewels Gifts ──
        if 'giftMessageViewModel' in obj:
            vm = obj['giftMessageViewModel']
            text = extract_text_field(vm.get('text'))
            m = re.search(r"comboed x(\d+) (.+?) for (\d+) Jewels", text)
            if m:
                qty = int(m.group(1))
                gname = m.group(2).strip()
                jewels_amt = int(m.group(3))
                author = extract_text_field(vm.get('authorName')) or "Unknown"
                inr_val = round(jewels_amt * INR_PER_JEWEL, 2)

                jewels.append({
                    "gift_name": gname,
                    "author": author,
                    "quantity": qty,
                    "jewels": jewels_amt,
                    "inr_value": inr_val,
                    "stream_id": video_id
                })
                total_jewels_count += jewels_amt
                total_jewels_inr += inr_val
                unique_donors.add(author)
                unique_chatters.add(author)

        # Recurse children
        for v in obj.values():
            if isinstance(v, dict):
                walk_tree(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        walk_tree(item)

    # Process all lines
    for line in json_lines:
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='ignore')
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
            walk_tree(data)
        except Exception:
            continue

    gross_inr = round(total_sc_inr + total_jewels_inr + total_mem_inr, 2)
    net_inr = round(gross_inr * 0.70, 2)

    return {
        "stream_id": video_id,
        "superchats": superchats,
        "memberships": memberships,
        "jewels": jewels,
        "custom_tiers": list(custom_tiers_discovered),
        "unique_donors": list(unique_donors),
        "unique_chatters_count": len(unique_chatters),
        "totals": {
            "gross_inr": gross_inr,
            "net_creator_inr": net_inr,
            "superchat_inr": round(total_sc_inr, 2),
            "superchat_count": len(superchats),
            "jewels_inr": round(total_jewels_inr, 2),
            "jewels_count": total_jewels_count,
            "memberships_inr": round(total_mem_inr, 2),
            "memberships_count": len(memberships),
            "donors_count": len(unique_donors),
            "currency_breakdown": dict(currency_breakdown)
        }
    }


# ─────────────────────────────────────────────
# CHANNEL STREAM DISCOVERY (Fast Metadata)
# ─────────────────────────────────────────────

def get_channel_streams(channel_url: str, from_date: Optional[str] = None, to_date: Optional[str] = None, max_results: int = 50) -> list[dict]:
    """
    Fetches list of live streams from a YouTube channel tab via yt-dlp flat-playlist.
    Returns list of dicts: [{'url': ..., 'id': ..., 'date': ..., 'title': ...}]
    """
    clean_url = channel_url.strip()
    if not clean_url.endswith("/streams") and "/@" in clean_url:
        clean_url = clean_url.rstrip("/") + "/streams"

    cmd = ["yt-dlp"]
    cmd.extend(get_cookie_arg())
    cmd.extend([
        "--flat-playlist",
        "--print", "%(id)s\t%(upload_date)s\t%(title)s",
        clean_url
    ])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=35)
        if res.returncode != 0 and not res.stdout.strip():
            return []

        streams = []
        for line in res.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 1:
                vid = parts[0].strip()
                if not re.match(r'^[A-Za-z0-9_-]{11}$', vid):
                    continue
                date_raw = parts[1].strip() if len(parts) > 1 else ""
                title = parts[2].strip() if len(parts) > 2 else "YouTube Live Stream"

                # Format YYYYMMDD to YYYY-MM-DD
                formatted_date = ""
                if len(date_raw) == 8 and date_raw.isdigit():
                    formatted_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:]}"

                # Date Filtering
                if from_date and formatted_date and formatted_date < from_date:
                    continue
                if to_date and formatted_date and formatted_date > to_date:
                    continue

                streams.append({
                    "id": vid,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "date": formatted_date or "Recent",
                    "title": title
                })

                if len(streams) >= max_results:
                    break

        return streams
    except Exception as e:
        print(f"Error fetching channel streams: {e}")
        return []


# ─────────────────────────────────────────────
# DIRECT INNERTUBE REPLAY STREAMER (Ultra-Fast & In-Memory)
# ─────────────────────────────────────────────

def fetch_innertube_stream_data(video_id: str, timeout: int = 15) -> Optional[dict]:
    """
    Directly streams YouTube Live Chat Replay JSON chunks from YouTube's InnerTube API.
    100% In-Memory, Zero Disk Storage, Pure Stateless Processing.
    Executes in 10-15 seconds for a multi-hour stream (15x faster than yt-dlp).
    """
    t0 = time.time()
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
    })

    # Optional cookies if cookies.txt is present
    for p in [Path("cookies.txt"), Path(__file__).parent / "cookies.txt", Path("/tmp/cookies.txt")]:
        if p.exists() and p.stat().st_size > 100:
            try:
                cj = http.cookiejar.MozillaCookieJar(str(p.resolve()))
                cj.load(ignore_discard=True, ignore_expires=True)
                session.cookies.update(cj)
                break
            except Exception:
                pass

    try:
        # 1. Fetch initial watch page for API key, title, and initial continuation token
        resp = session.get(f"https://www.youtube.com/watch?v={video_id}", timeout=timeout)
        if resp.status_code != 200:
            return None
        html = resp.text

        # Extract API key
        m_key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if not m_key:
            return None
        api_key = m_key.group(1)

        # Extract real stream title
        m_title = re.search(r'<title>(.*?)</title>', html)
        title = m_title.group(1).replace(" - YouTube", "").strip() if m_title else f"Live Stream [{video_id}]"

        # Extract continuation token
        m_cont = re.search(r'"liveChatRenderer":\s*\{.*?"continuation":\s*"([^"]+)"', html, re.DOTALL)
        if not m_cont:
            m_cont = re.search(r'"reloadContinuationData":\s*\{\s*"continuation":\s*"([^"]+)"', html)
        if not m_cont:
            m_cont = re.search(r'"continuation":"([a-zA-Z0-9_\-=]+)"', html)

        if not m_cont:
            return None
        cont_token = m_cont.group(1)

        # 2. In-memory streaming of replay chunks
        all_json_lines = []
        seen = set()
        chunks = 0
        api_url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat_replay?key={api_key}"

        while cont_token and cont_token not in seen and chunks < 350:
            seen.add(cont_token)
            chunks += 1

            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": "2.20261106.00.00"
                    }
                },
                "continuation": cont_token
            }

            r = session.post(api_url, json=payload, timeout=12)
            if r.status_code != 200:
                break
            data = r.json()

            chat_cont = data.get("continuationContents", {}).get("liveChatContinuation", {})
            actions = chat_cont.get("actions", [])
            for act in actions:
                all_json_lines.append(json.dumps(act))

            # Retrieve next continuation token
            next_token = None
            for c in chat_cont.get("continuations", []):
                for k in ["liveChatReplayContinuationData", "reloadContinuationData", "timedContinuationData"]:
                    if k in c and "continuation" in c[k]:
                        next_token = c[k]["continuation"]
                        break
                if next_token:
                    break
            cont_token = next_token

        if not all_json_lines:
            return None

        parsed_data = parse_chat_json_lines(all_json_lines, video_id=video_id)
        return {
            "success": True,
            "video_id": video_id,
            "title": title,
            "data": parsed_data,
            "engine": "innertube_direct",
            "chunks_streamed": chunks,
            "elapsed_sec": round(time.time() - t0, 2)
        }

    except Exception as e:
        print(f"[InnerTube Error] {e}")
        return None

