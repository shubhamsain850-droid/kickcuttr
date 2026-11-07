"""
YouTube Live Stream Earning, Super Chat, Jewels & Chat Analytics Engine
Handles:
- Channel streams discovery (via yt-dlp flat-playlist) with date filtering
- Live Chat replay extraction & streaming
- Deduplicated Super Chat parsing (amounts, currencies, messages, donors)
- Dynamic Custom Membership Tier detection (e.g., 'Welcome to [Tier]!')
- YouTube Jewels mapping (Panipuri, Samosa, Mic drop, Castle etc. to INR)
- Clean JSON / CSV export formatting
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────
# CONSTANTS & MAPS (From memberv4.py & research)
# ─────────────────────────────────────────────

INR_PER_JEWEL: float = 1.20  # Based on standard Rs.4799 / 4000 jewels pack

JEWEL_MAP: dict[str, int] = {
    # 2 Jewels
    "Hamsasya mudra": 2, "Panipuri": 2, "Mango lassi": 2, "Samosa": 2,
    "Star": 2, "Namaste": 2, "Sparkles": 2,
    # 5–10 Jewels
    "Kem cho?": 5, "Macha": 5, "Arre yaar!": 5, "Chili": 6,
    "Cricket": 10, "All izz well": 10, "Gold coin": 10, "Press F": 10, "Bas kar": 10,
    # 20–98 Jewels
    "Red card": 20, "Party hat": 20, "GGEZ": 20, "Argentina": 98, "Six seven": 67, "Clock it": 80,
    # 100–200 Jewels
    "Chai toast": 100, "Vada pav": 100, "Vibe hai": 100, "Sunshine": 200,
    # 250–400 Jewels
    "Clapping seal": 250, "Laughing disco": 250, "Girl power": 300,
    "Aiyyo!": 350, "Bindaas": 350, "Finger heart": 350, "Fortune cookie": 380, "Badhai ho": 400,
    # 450–800 Jewels
    "Butterfly": 450, "Watermelon": 500, "Tulips": 500, "Power potion": 500, "Soccer trophy": 500,
    "Picnic basket": 600, "Mic drop": 600, "Mystery gift": 650, "Nap": 680, "Island": 700,
    "Let em cook": 750, "Husky": 800, "GG keys": 800,
    # 900–1500 Jewels
    "Duckling": 900, "Guitar": 1000, "Trophy cake": 1000, "Goat trophy": 1000,
    "Corgi": 1000, "Park the bus": 1000, "Sand castle": 1200, "Bumblebee": 1250,
    "Tube dancer": 1500, "Bubble heart": 1500,
    # 3000–5000 Jewels
    "Beach dog": 3000, "Headphones": 3600, "Hound": 3800, "Biscuit tin": 5000,
    # 5500+ Jewels
    "Sushi": 5500, "Hot pot": 6800, "Hooray": 7000, "Rock star": 7800, "Sports car": 9500,
    "Trifle smash": 10000, "Limo": 10000, "Seal splash": 12000, "Ball party": 12000,
    "Gamer corgi": 15000, "Night market": 15500, "Victory spin": 17000, "Castle": 18000,
    "Racing game": 20000, "Seaside": 22500,
}

CURRENCY_RATES_TO_INR: dict[str, float] = {
    "INR": 1.0, "₹": 1.0, "Rs": 1.0,
    "USD": 86.5, "$": 86.5, "US$": 86.5,
    "EUR": 93.0, "€": 93.0,
    "GBP": 110.0, "£": 110.0,
    "CAD": 63.5, "C$": 63.5,
    "AUD": 56.0, "A$": 56.0,
    "AED": 23.5,
    "SGD": 64.5,
    "MYR": 18.5,
    "PHP": 1.5,
    "BDT": 0.72,
    "JPY": 0.58, "¥": 0.58,
    "KRW": 0.063, "₩": 0.063
}

# Standard fallback prices for Indian tiers when channel tier price is unknown
DEFAULT_TIER_PRICES: dict[str, float] = {
    "Level 1": 59.0,
    "Level 2": 159.0,
    "Level 3": 299.0,
    "Level 4": 799.0,
    "Level 5": 3999.0,
}

# ─────────────────────────────────────────────
# RECURSIVE JSON TRAVERSAL HELPER
# ─────────────────────────────────────────────

def find_key(obj, key: str):
    """Recursively search for all values matching key in nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            yield obj[key]
        for v in obj.values():
            yield from find_key(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from find_key(item, key)


def extract_video_id(url_or_id: str) -> str:
    """Extract clean 11-char YouTube video ID."""
    url = url_or_id.strip()
    m = re.search(r'(?:v=|\/|be\/|shorts\/)([0-9A-Za-z_-]{11})', url)
    return m.group(1) if m else url


def parse_amount(text: str) -> tuple[float, str]:
    """Parse amount and currency symbol from string (e.g. '₹101.00', '$1.99', '100 INR')."""
    if not text:
        return 0.0, "INR"
    
    # Currency symbol
    currency = "INR"
    for sym, curr in [("₹", "INR"), ("Rs", "INR"), ("US$", "USD"), ("$", "USD"),
                      ("€", "EUR"), ("£", "GBP"), ("C$", "CAD"), ("A$", "AUD")]:
        if sym in text:
            currency = curr
            break

    # Numeric part
    clean = re.sub(r'[^\d.]', '', text)
    try:
        val = float(clean)
    except ValueError:
        val = 0.0

    return val, currency


# ─────────────────────────────────────────────
# CORE CHAT & STREAM PARSER
# ─────────────────────────────────────────────

def parse_chat_json_lines(json_lines, video_id: str = "") -> dict:
    """
    Parses live chat JSON lines (from file or memory), deduplicating actions by ID.
    Extracts:
    - Superchats
    - Memberships (Dynamic Tiers + Renewals)
    - YouTube Jewels Gifts
    """
    seen_ids = set()
    superchats = []
    memberships = []
    jewels = []
    unique_donors = set()
    unique_chatters = set()
    custom_tiers_discovered = set()

    total_sc_inr = 0.0
    total_jewels_count = 0
    total_jewels_inr = 0.0
    total_mem_inr = 0.0

    for line in json_lines:
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='ignore')
        line = line.strip()
        if not line:
            continue

        try:
            data = json.loads(line)
        except Exception:
            continue

        # ── 1. Superchats ──
        if 'purchaseAmountText' in line:
            for sc in find_key(data, 'liveChatPaidMessageRenderer'):
                sc_id = sc.get('id')
                if sc_id and sc_id in seen_ids:
                    continue
                if sc_id:
                    seen_ids.add(sc_id)

                author = sc.get('authorName', {}).get('simpleText', 'Unknown')
                amt_text = sc.get('purchaseAmountText', {}).get('simpleText', '')
                amt, curr = parse_amount(amt_text)
                rate = CURRENCY_RATES_TO_INR.get(curr, 1.0)
                amt_inr = round(amt * rate, 2)

                msg_runs = sc.get('message', {}).get('runs', [])
                msg = ''.join(r.get('text', '') for r in msg_runs)
                time_text = sc.get('timestampText', {}).get('simpleText', '')

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
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 2. Memberships (Dynamic Custom Tier Detection) ──
        if 'liveChatMembershipItemRenderer' in line:
            for mem in find_key(data, 'liveChatMembershipItemRenderer'):
                mem_id = mem.get('id')
                if mem_id and mem_id in seen_ids:
                    continue
                if mem_id:
                    seen_ids.add(mem_id)

                author = mem.get('authorName', {}).get('simpleText', 'Unknown')
                header_sub = mem.get('headerSubtext', {}).get('runs', [])
                header_pri = mem.get('headerPrimaryText', {}).get('runs', [])
                
                sub_text = ''.join(r.get('text', '') for r in header_sub)
                pri_text = ''.join(r.get('text', '') for r in header_pri)

                tier = "General Member"
                m_type = "Member"
                est_price = 59.0  # Base level 1 fallback

                if 'welcome to' in sub_text.lower():
                    # Exact dynamic extraction of streamer's custom tier
                    tier = re.sub(r'(?i)welcome to\s*', '', sub_text).rstrip('!').strip()
                    m_type = "New Join"
                    custom_tiers_discovered.add(tier)
                    est_price = 59.0
                elif 'upgraded to' in sub_text.lower():
                    tier = re.sub(r'(?i)upgraded to\s*', '', sub_text).rstrip('!').strip()
                    m_type = "Upgrade"
                    custom_tiers_discovered.add(tier)
                    est_price = 299.0
                elif pri_text:
                    tier = pri_text.strip()
                    m_type = "Milestone / Renewal"
                    est_price = 59.0

                memberships.append({
                    "id": mem_id or "",
                    "author": author,
                    "type": m_type,
                    "tier": tier,
                    "price_inr": est_price,
                    "stream_id": video_id
                })
                total_mem_inr += est_price
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 3. Gifted Memberships ──
        if 'liveChatSponsorshipsGiftPurchaseAnnouncementRenderer' in line:
            for gift in find_key(data, 'liveChatSponsorshipsGiftPurchaseAnnouncementRenderer'):
                g_id = gift.get('id')
                if g_id and g_id in seen_ids:
                    continue
                if g_id:
                    seen_ids.add(g_id)

                header = gift.get('header', {}).get('liveChatSponsorshipsHeaderRenderer', {})
                author = header.get('authorName', {}).get('simpleText', 'Unknown')
                gift_count = 1
                for r in header.get('primaryText', {}).get('runs', []):
                    txt = r.get('text', '').strip()
                    if txt.isdigit():
                        gift_count = int(txt)
                        break

                est_gift_val = gift_count * 59.0
                memberships.append({
                    "id": g_id or "",
                    "author": author,
                    "type": "Gifted Membership",
                    "tier": f"{gift_count} Memberships Gifted",
                    "price_inr": est_gift_val,
                    "stream_id": video_id
                })
                total_mem_inr += est_gift_val
                unique_donors.add(author)
                unique_chatters.add(author)

        # ── 4. YouTube Jewels Gifts & Stickers ──
        if 'giftMessageViewModel' in line:
            for vm in find_key(data, 'giftMessageViewModel'):
                text = vm.get('text', {}).get('content', '')
                m = re.search(r"comboed x(\d+) (.+?) for (\d+) Jewels", text)
                if m:
                    qty = int(m.group(1))
                    gname = m.group(2).strip()
                    jewels_amt = int(m.group(3))
                    author = vm.get('authorName', {}).get('content', 'Unknown').strip()
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

        # ── 5. Paid Stickers (Jewels single gift) ──
        if 'liveChatPaidStickerRenderer' in line:
            for sticker in find_key(data, 'liveChatPaidStickerRenderer'):
                s_id = sticker.get('id')
                if s_id and s_id in seen_ids:
                    continue
                if s_id:
                    seen_ids.add(s_id)

                author = sticker.get('authorName', {}).get('simpleText', 'Unknown')
                runs = sticker.get('purchaseText', {}).get('runs', [])
                gname = next((r.get('text', '') for r in runs if r.get('text', '') not in ("Sent ", "")), "Sticker")
                j_cost = JEWEL_MAP.get(gname, 10)
                inr_val = round(j_cost * INR_PER_JEWEL, 2)

                jewels.append({
                    "gift_name": gname,
                    "author": author,
                    "quantity": 1,
                    "jewels": j_cost,
                    "inr_value": inr_val,
                    "stream_id": video_id
                })
                total_jewels_count += j_cost
                total_jewels_inr += inr_val
                unique_donors.add(author)
                unique_chatters.add(author)

    gross_inr = round(total_sc_inr + total_jewels_inr + total_mem_inr, 2)
    net_inr = round(gross_inr * 0.70, 2)  # Creator 70% share

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
            "donors_count": len(unique_donors)
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

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--print", "%(id)s\t%(upload_date)s\t%(title)s",
        clean_url
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=25)
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
