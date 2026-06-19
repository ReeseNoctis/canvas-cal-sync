#!/usr/bin/env python3
"""
Canvas → Apple Calendar Sync
Sync assignments, OH, and RC events from Canvas LMS to Apple Calendar via REST API.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import os
import requests
from openai import OpenAI

PROJECT_DIR = Path(__file__).parent
DATA_DIR = PROJECT_DIR / "data"
CONFIG_PATH = PROJECT_DIR / "config.json"
TOKEN_PATH = DATA_DIR / "api_token.txt"
LAST_SYNC_PATH = DATA_DIR / "last_sync.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://oc.sjtu.edu.cn/api/v1"

# ---- Weekday mappings ----
WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
WEEKDAY_EN = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# ---- Keyword matching ----
_WORD_BOUNDARY_KW = {"RC", "OH", "Exam", "final"}
_WORD_BOUNDARY_KW_LOWER = frozenset(kw.lower() for kw in _WORD_BOUNDARY_KW)


def _keyword_match(keyword, line):
    """Check if keyword appears in line.

    Uses word-boundary matching for bare short keywords (RC, OH, Exam, final)
    to avoid false positives on substrings like "source", "research", "example", "finally".
    Falls back to substring matching for longer/punctuated keywords.
    """
    if keyword.lower() in _WORD_BOUNDARY_KW_LOWER:
        return bool(re.search(r'\b' + re.escape(keyword) + r'\b', line, re.IGNORECASE))
    return keyword.lower() in line.lower()


# ---- LLM-based event extraction ----

LLM_KEY_PATH = DATA_DIR / "api_key_llm.txt"


def get_llm_api_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key and LLM_KEY_PATH.exists():
        key = LLM_KEY_PATH.read_text().strip()
    return key


def call_deepseek(config, system_prompt, user_prompt):
    """Call DeepSeek API with retry logic."""
    api_key = get_llm_api_key()
    if not api_key:
        raise RuntimeError("DeepSeek API key not found. Set DEEPSEEK_API_KEY env var or write to data/api_key_llm.txt")

    client = OpenAI(api_key=api_key, base_url=config["llm"]["base_url"])

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=config["llm"]["model"],
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=4096,
                timeout=60,
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt < 2:
                print(f"  [LLM Retry {attempt+1}] {e}")
                import time
                time.sleep(2 ** attempt)
            else:
                raise


def build_extraction_prompt(text_segments, student_id=""):
    """Build user prompt with all course text segments."""
    header = ""
    if student_id:
        header = (
            f"Student's ID number: {student_id}\n\n"
            f"When exam locations specify student ID ranges (e.g., 'odd IDs go to Room A, "
            f"even IDs to Room B', 'ID 001-050 in Room 1'), use the student's ID to "
            f"determine the correct room and include it in the \"location\" field.\n\n"
        )
    parts = []
    for seg in text_segments:
        text = seg["text"][:2000]  # truncate per segment
        parts.append(
            f"--- Course: {seg['course_name']} ---\n"
            f"Source: {seg['source']}\n"
            f"Text: {text}\n"
        )
    return header + "\n".join(parts)


def parse_llm_json(text):
    """Extract JSON array from LLM response (may be wrapped in markdown)."""
    if not text:
        return []
    text = text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "events" in data:
            return data["events"]
        return []
    except json.JSONDecodeError:
        # Try to find JSON array in text
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        print(f"  [LLM Parse Error] Could not parse response: {text[:200]}...")
        return []


def extract_events_llm(config, courses):
    """Extract OH/RC/Exam events from courses using LLM (DeepSeek).

    Returns (events_list, cancel_list) where:
      events_list: list of event dicts to add to calendar
      cancel_list: list of identifying dicts for events to cancel

    Returns (None, None) on failure (caller should fall back to regex).
    """
    # Collect text segments that contain OH/RC/Exam keywords (pre-filter)
    all_kw = (config.get("oh_keywords", [])
              + config.get("rc_keywords", [])
              + config.get("exam_keywords", []))
    text_segments = []

    for course in courses:
        course_name = course["name"]

        # Syllabus
        syllabus = get_syllabus_text(course)
        if syllabus:
            text_segments.append({
                "course_name": course_name, "source": "syllabus", "text": syllabus,
            })

        # Pages
        for p in get_page_texts(course):
            text_segments.append({
                "course_name": course_name, "source": f"page: {p['title']}", "text": p["text"],
            })

        # Announcements
        for ann in get_announcements(course):
            text_segments.append({
                "course_name": course_name, "source": f"announcement: {ann['title']}", "text": ann["text"],
            })

    # Filter to only segments that mention OH/RC keywords (cheap pre-filter)
    relevant = []
    for seg in text_segments:
        if any(kw.lower() in seg["text"].lower() for kw in all_kw):
            relevant.append(seg)

    if not relevant:
        print("[LLM] No text segments with OH/RC/Exam keywords found")
        return [], []

    print(f"[LLM] Sending {len(relevant)} text segments to DeepSeek...")

    student_id = config.get("student_id", "")
    user_prompt = build_extraction_prompt(relevant, student_id)

    system_prompt = """You are an event extraction system for a university course calendar. Extract scheduled OH (Office Hour), RC (Recitation Class), and Exam events from course materials.

Rules:
1. Only extract events with specific dates AND times. SKIP surveys, polls, forms, group chat invites, recording links, and general non-schedule announcements.
2. STUDENT ID ROOM ASSIGNMENT: The student's ID number is provided in the prompt below. When exam locations specify student ID ranges (e.g., "odd IDs go to Room A, even IDs to Room B", "ID 001-050 in Room 1, 051-100 in Room 2", "学号单号去A教室，双号去B教室"), use the student's ID to determine the correct room and include it in the "location" field. If no such ranges exist, use the location as stated.
3. CANCEL events: When an event is explicitly cancelled, use action "cancel". Set day_of_week and start_time to the OLD schedule's values.
4. RESCHEDULED events: When you see "shifted from X to Y", "moved to", or "rescheduled to", create:
   - An "add" event for the NEW time (with date, start_time, end_time filled).
   - A "cancel" event for the OLD time (with action="cancel", day_of_week=OLD weekday, start_time=OLD time).
   Example: "RC shifted from Monday to Wednesday May 27, 20:20" →
     add: {action:"add", date:"2026-05-27", day_of_week:2, start_time:"20:20", end_time:"21:50"}
     cancel: {action:"cancel", day_of_week:0, start_time:"20:20", end_time:"21:50"}
5. Convert ALL times to 24h format. "8:20 PM" → "20:20". "4:00 PM" → "16:00".
6. For recurring weekly events (e.g., "every Monday 20:20", "Mondays, 20:20"): set day_of_week (0=Mon, 6=Sun), leave date null. ONLY use this for OH/RC — never for Exam events.
7. For one-time specific-date events (e.g., "May 27, 20:20"): include the full date as YYYY-MM-DD, set day_of_week to the correct weekday number.
8. Today is """ + datetime.now().strftime("%Y-%m-%d") + """ (""" + datetime.now().strftime("%A") + """). Use this to resolve dates like "May 27" → 2026-05-27. Skip events more than 7 days in the past.
9. Extract location/room if mentioned (e.g., ZY103, DZY4-201, lbl 326A, DSY215, 东中院1-200).
10. If a course has BOTH a recurring weekly pattern AND announcements about specific date changes, the specific-date announcement overrides the recurring pattern for that date.
11. Exam events: ALWAYS provide a specific date (never use day_of_week without date). Default duration is 2 hours if only the start time is given. Never create recurring Exam events.
12. ALWAYS provide both start_time and end_time as "HH:MM". If only a start time is mentioned, estimate the end time as 2 hours later. Never leave start_time or end_time as null.
13. Return ONLY a JSON array. No markdown, no explanation text.

JSON schema:
[
  {
    "course_name": "string",
    "type": "OH" or "RC" or "Exam",
    "action": "add" or "cancel" or "reschedule",
    "title": "short event title",
    "date": "YYYY-MM-DD" or null,
    "start_time": "HH:MM (24h)",
    "end_time": "HH:MM (24h)",
    "day_of_week": 0-6 or null,
    "location": "string or empty",
    "notes": "brief note",
    "cancel_date": "YYYY-MM-DD" or null,
    "cancel_start_time": "HH:MM" or null,
    "cancel_day_of_week": 0-6 or null
  }
]

Example Exam extraction:
Input: "Final Exam: June 20, 14:00-16:00 in Room ZY103. Students with odd IDs go to DZY4-201."
Student ID: 123456 (even) → uses DZY4-201
Output: {"course_name": "ECE2160", "type": "Exam", "action": "add", "title": "Final Exam", "date": "2026-06-20", "start_time": "14:00", "end_time": "16:00", "day_of_week": 5, "location": "DZY4-201", "notes": "Exam room by student ID range"}

Chinese example: "期末考试 6月20日 14:00-16:00 地点：东中院1-200"
Output: {"course_name": "ECE2160", "type": "Exam", "action": "add", "title": "期末考试", "date": "2026-06-20", "start_time": "14:00", "end_time": "16:00", "day_of_week": 5, "location": "东中院1-200", "notes": ""}

Return [] if no events found."""

    try:
        response_text = call_deepseek(config, system_prompt, user_prompt)
        raw_events = parse_llm_json(response_text)
    except Exception as e:
        print(f"[LLM Error] {e}")
        return None, None  # signal fallback

    if not raw_events:
        print("[LLM] No events extracted")
        return [], []

    # Convert LLM response to calendar-compatible event dicts
    events = []
    cancels = []

    for e in raw_events:
        action = e.get("action", "add")
        course_name = e.get("course_name", "")
        etype = e.get("type", "RC")
        llm_title = e.get("title", "")
        title = f"[{etype}] {course_name} - {llm_title}" if llm_title else f"[{etype}] {course_name}"
        location = e.get("location", "")
        notes = e.get("notes", "")
        day_of_week = e.get("day_of_week")

        # Parse start/end times with null guards
        start_str = e.get("start_time")
        if not start_str:
            continue  # skip events without start time
        try:
            start_h, start_m = map(int, start_str.split(":"))
        except (ValueError, AttributeError):
            continue

        end_str = e.get("end_time")
        if end_str:
            try:
                end_h, end_m = map(int, end_str.split(":"))
            except (ValueError, AttributeError):
                end_h, end_m = start_h + 2, start_m  # default 2h duration
        else:
            end_h, end_m = start_h + 2, start_m  # default 2h duration

        date_str = e.get("date")

        if date_str:
            try:
                start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
                end = datetime.strptime(f"{date_str} {end_h:02d}:{end_m:02d}", "%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                continue
        elif day_of_week is not None:
            start = find_next_weekday(day_of_week).replace(hour=start_h, minute=start_m, second=0)
            end = find_next_weekday(day_of_week).replace(hour=end_h, minute=end_m, second=0)
        else:
            continue  # need either date or day_of_week

        event_dict = {
            "type": etype,
            "title": title,
            "start": start,
            "end": end,
            "day_of_week": start.weekday(),
            "is_absolute": date_str is not None,
            "raw": notes or f"LLM extracted: {e.get('date', '')} {start_str}",
            "course_name": course_name,
            "location": location,
        }

        # Exam events must be specific-date (never recurring)
        if etype == "Exam" and not event_dict.get("is_absolute"):
            print(f"  [LLM Skip] Exam without specific date: {title}")
            continue

        if action in ("cancel", "reschedule"):
            # Build cancel target using the event's own fields
            cancel_target = {
                "course_name": course_name,
                "type": etype,
                "day_of_week": day_of_week,
                "start_hour": start_h if day_of_week is not None else start.hour,
            }
            # Also try cancel_* fields for more precision
            cancel_dow = e.get("cancel_day_of_week")
            if cancel_dow is not None:
                cancel_target["day_of_week"] = cancel_dow
            cancel_time = e.get("cancel_start_time")
            if cancel_time:
                try:
                    ch, _ = map(int, cancel_time.split(":"))
                    cancel_target["start_hour"] = ch
                except (ValueError, AttributeError):
                    pass
            cancels.append(cancel_target)

        if action in ("add", "reschedule"):
            events.append(event_dict)

    # Auto-cancel: a specific-date event on the same weekday supersedes a recurring one
    # Only cancels when the specific event falls on the same day of week as the recurring pattern.
    # Cross-weekday shifts (e.g. Monday→Wednesday) are handled by the LLM via explicit cancel events.
    specific_events = [e for e in events if e.get("is_absolute")]
    recurring_events = [e for e in events if not e.get("is_absolute")]
    for sp in specific_events:
        for rec in recurring_events:
            if (sp["course_name"] == rec["course_name"]
                and sp["type"] == rec["type"]
                and sp["day_of_week"] == rec["day_of_week"]):
                days_diff = (sp["start"].date() - rec["start"].date()).days
                if abs(days_diff) <= 7:
                    time_sp = sp["start"].hour * 60 + sp["start"].minute
                    time_rec = rec["start"].hour * 60 + rec["start"].minute
                    if abs(time_sp - time_rec) <= 90:  # within 1.5 hours
                        cancels.append({
                            "course_name": rec["course_name"],
                            "type": rec["type"],
                            "day_of_week": rec["day_of_week"],
                            "start_hour": rec["start"].hour,
                        })
                        print(f"  [Auto-Cancel] {rec['course_name']} {rec['type']} recurring day={rec['day_of_week']} {rec['start']:%H:%M} (superseded by {sp['start']:%Y-%m-%d %H:%M})")

    print(f"[LLM] Extracted {len(events)} events, {len(cancels)} cancellations")

    for ev in events:
        print(f"  + [{ev['type']}] {ev['course_name']}: {ev['start']:%Y-%m-%d %a %H:%M} - {ev['end']:%H:%M}  @ {ev['location']}")
    for ca in cancels:
        print(f"  - [CANCEL] {ca['course_name']}: weekday={ca['day_of_week']}, hour={ca['start_hour']}")

    return events, cancels


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_token():
    if not TOKEN_PATH.exists():
        print("[Error] API token not found. Write your Canvas token to data/api_token.txt")
        sys.exit(1)
    return TOKEN_PATH.read_text().strip()


def api_get(path, params=None):
    """Call Canvas REST API"""
    if params is None:
        params = {}
    params["per_page"] = 100
    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {get_token()}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)

    if resp.status_code == 403:
        print(f"[API Error] 403 Forbidden — token may be expired or lacks permission: {path}")
        return []
    if resp.status_code == 404:
        print(f"[API Warn] 404 Not Found: {path}")
        return []
    resp.raise_for_status()
    return resp.json()


# ==============================
# Canvas API data fetching
# ==============================

def get_courses():
    """Get active courses for the current term"""
    courses = api_get("/courses", {"enrollment_state": "active",
                                    "include[]": ["term"]})
    results = []
    for c in courses:
        name = c.get("name", "")
        if not name or "Template" in name:
            continue
        results.append({
            "id": c["id"],
            "name": name,
            "course_code": c.get("course_code", ""),
        })

    print(f"[API] Found {len(results)} courses")
    for c in results:
        print(f"  - {c['name']} (id={c['id']})")
    return results


def get_assignments(course):
    """Get assignments with due dates for a course"""
    try:
        items = api_get(f"/courses/{course['id']}/assignments")
    except Exception as e:
        print(f"  [Warn] Failed to get assignments for {course['name']}: {e}")
        return []

    assignments = []
    for a in items:
        due = a.get("due_at")
        if not due:
            continue
        due_dt = datetime.fromisoformat(due.replace("Z", "+00:00")).replace(tzinfo=None)
        assignments.append({
            "title": a["name"],
            "due_date": due_dt,
            "course_name": course["name"],
            "url": a.get("html_url", ""),
        })
    return assignments


def get_syllabus_text(course):
    """Get the syllabus body text for a course"""
    try:
        data = api_get(f"/courses/{course['id']}?include[]=syllabus_body")
        if isinstance(data, dict):
            body = data.get("syllabus_body", "")
            if body:
                return re.sub(r"<[^>]+>", " ", body)
    except Exception:
        pass
    return ""


def get_announcements(course):
    """Get recent announcements for a course"""
    try:
        items = api_get(f"/courses/{course['id']}/discussion_topics",
                        {"only_announcements": "true"})
    except Exception:
        return []

    announcements = []
    for a in items[:10]:
        title = a.get("title", "")
        message = a.get("message", "")
        if message:
            message = re.sub(r"<[^>]+>", " ", message)
        posted = a.get("posted_at", "")
        announcements.append({
            "title": title,
            "text": f"{title}\n{message}",
            "posted_at": posted,
        })
    return announcements


def get_page_texts(course):
    """Get all page contents for a course (up to 20 pages)"""
    try:
        pages = api_get(f"/courses/{course['id']}/pages")
    except Exception:
        return []

    texts = []
    for p in pages[:20]:
        title = p.get("title", "").lower()
        url = p.get("url", "")
        try:
            detail = api_get(f"/courses/{course['id']}/pages/{url}")
            if isinstance(detail, dict):
                body = detail.get("body", "")
                if body:
                    body = re.sub(r"<[^>]+>", " ", body)
                    texts.append({"title": title, "text": body})
        except Exception:
            continue
    return texts


# ==============================
# Time parsing (regex fallback)
# ==============================

def find_next_weekday(target_weekday, after=None):
    after = after or datetime.now()
    days_ahead = target_weekday - after.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return after + timedelta(days=days_ahead)


def parse_weekday_time(text):
    """Parse weekday + time patterns like 'Wednesday 14:00-16:00' or '周三 14:00'"""
    results = []
    # Chinese weekday patterns: 周三 14:00-16:00 / 每周三 10:00
    for m in re.finditer(
        r"(?:每\s*周\s*)?(?:星期|周)\s*([一二三四五六日天])\s*"
        r"(\d{1,2}):(\d{2})\s*[-~—至到]\s*(\d{1,2}):(\d{2})",
        text,
    ):
        day_cn, sh, sm, eh, em = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        if day_cn in WEEKDAY_CN:
            results.append({
                "day_of_week": WEEKDAY_CN[day_cn],
                "start_h": sh, "start_m": sm,
                "end_h": eh, "end_m": em,
                "raw": m.group(0),
            })

    # Start time only (no end time) — assume 2-hour duration
    for m in re.finditer(
        r"(?:每\s*周\s*)?(?:星期|周)\s*([一二三四五六日天])\s*"
        r"(\d{1,2}):(\d{2})(?!\s*[-~—至到])",
        text,
    ):
        day_cn, sh, sm = m.group(1), int(m.group(2)), int(m.group(3))
        if day_cn in WEEKDAY_CN:
            results.append({
                "day_of_week": WEEKDAY_CN[day_cn],
                "start_h": sh, "start_m": sm,
                "end_h": sh + 2, "end_m": sm,
                "raw": m.group(0),
            })

    # English: Monday 14:00-16:00
    en_days = "|".join(WEEKDAY_EN.keys())
    for m in re.finditer(
        rf"({en_days})\s*(\d{{1,2}}):(\d{{2}})\s*[-~—至到]\s*(\d{{1,2}}):(\d{{2}})",
        text, re.IGNORECASE,
    ):
        day_en, sh, sm, eh, em = m.group(1).lower(), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        if day_en in WEEKDAY_EN:
            results.append({
                "day_of_week": WEEKDAY_EN[day_en],
                "start_h": sh, "start_m": sm,
                "end_h": eh, "end_m": em,
                "raw": m.group(0),
            })
    return results


def parse_absolute_time(text):
    """Parse absolute date+time patterns like 'Wednesday, 27th May, 20:20-21:50'.

    Returns list of dicts with start_datetime, end_datetime, raw.
    If no end time is given, defaults to 2-hour duration.
    """

    def _resolve_year(month, day):
        """Pick current year, or next year if date is >60 days in the past."""
        now = datetime.now()
        try:
            dt = datetime(now.year, month, day)
        except ValueError:
            return now.year
        if (now - dt).days > 60:
            return now.year + 1
        return now.year

    months = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    mon_names = "|".join(months.keys())
    results = []
    seen = set()

    # A: ISO date YYYY-MM-DD HH:MM[-HH:MM]
    for m in re.finditer(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+"
        r"(\d{1,2}):(\d{2})"
        r"(?:\s*[-–]\s*(\d{1,2}):(\d{2}))?",
        text,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            start = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                             int(m.group(4)), int(m.group(5)))
            if m.group(6):
                end = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                               int(m.group(6)), int(m.group(7)))
            else:
                end = start + timedelta(hours=2)
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    # B: [Weekday,] N(st|nd|rd|th) Month, HH:MM-HH:MM
    for m in re.finditer(
        rf"(?:[A-Z][a-z]+day,?\s*)?"
        rf"(\d{{1,2}})(?:st|nd|rd|th)\s+({mon_names})(?:,|\s)\s*"
        rf"(\d{{1,2}}):(\d{{2}})\s*[-–]\s*(\d{{1,2}}):(\d{{2}})",
        text, re.IGNORECASE,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            day, mon = int(m.group(1)), months[m.group(2).lower()]
            year = _resolve_year(mon, day)
            start = datetime(year, mon, day, int(m.group(3)), int(m.group(4)))
            end = datetime(year, mon, day, int(m.group(5)), int(m.group(6)))
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    # C: [Weekday,] N(st|nd|rd|th) Month, HH:MM (no end time)
    for m in re.finditer(
        rf"(?:[A-Z][a-z]+day,?\s*)?"
        rf"(\d{{1,2}})(?:st|nd|rd|th)\s+({mon_names})(?:,|\s)\s*"
        rf"(\d{{1,2}}):(\d{{2}})"
        rf"(?!\s*[-–]\s*\d{{1,2}}:\d{{2}})",
        text, re.IGNORECASE,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            day, mon = int(m.group(1)), months[m.group(2).lower()]
            year = _resolve_year(mon, day)
            start = datetime(year, mon, day, int(m.group(3)), int(m.group(4)))
            end = start + timedelta(hours=2)
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    # D: Month N(st|nd|rd|th)?, HH:MM-HH:MM
    for m in re.finditer(
        rf"({mon_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*"
        rf"(\d{{1,2}}):(\d{{2}})\s*[-–]\s*(\d{{1,2}}):(\d{{2}})",
        text, re.IGNORECASE,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            mon, day = months[m.group(1).lower()], int(m.group(2))
            year = _resolve_year(mon, day)
            start = datetime(year, mon, day, int(m.group(3)), int(m.group(4)))
            end = datetime(year, mon, day, int(m.group(5)), int(m.group(6)))
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    # E: Month N(st|nd|rd|th)?, HH:MM (no end time)
    for m in re.finditer(
        rf"({mon_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*"
        rf"(\d{{1,2}}):(\d{{2}})"
        rf"(?!\s*[-–]\s*\d{{1,2}}:\d{{2}})",
        text, re.IGNORECASE,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            mon, day = months[m.group(1).lower()], int(m.group(2))
            year = _resolve_year(mon, day)
            start = datetime(year, mon, day, int(m.group(3)), int(m.group(4)))
            end = start + timedelta(hours=2)
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    # F: N Month HH:MM[-HH:MM] (compact, no ordinal)
    for m in re.finditer(
        rf"(\d{{1,2}})\s+({mon_names})(?:\s+|\s*,\s*)"
        rf"(\d{{1,2}}):(\d{{2}})"
        rf"(?:\s*[-–]\s*(\d{{1,2}}):(\d{{2}}))?",
        text, re.IGNORECASE,
    ):
        raw = m.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        try:
            day, mon = int(m.group(1)), months[m.group(2).lower()]
            year = _resolve_year(mon, day)
            start = datetime(year, mon, day, int(m.group(3)), int(m.group(4)))
            if m.group(5):
                end = datetime(year, mon, day, int(m.group(5)), int(m.group(6)))
            else:
                end = start + timedelta(hours=2)
        except ValueError:
            continue
        results.append({"start_datetime": start, "end_datetime": end, "raw": raw})

    return results


def extract_location(text):
    """Extract room/building location from text (supports SJTU naming conventions)"""
    patterns = [
        # Full Chinese building names
        r"(东[上下中]院\s*\d+[-–]\d+)",        # e.g. 东中院1-200
        r"([上下中]院\s*\d+[-–]\d+)",           # e.g. 上院1-100
        r"(东[上下中]院\s*\d+)",                # e.g. 东中院201
        r"([上下中]院\s*\d+)",                  # e.g. 上院105
        # Chinese abbreviations: 东中1-200 / 东上2-301
        r"(东[中上下]\s*\d+[-–]\d+)",
        r"(东[中上下]\s*\d+)",
        # Pinyin abbreviations: DZY1-200 / DSY2-301 / DXY1-100
        r"([Dd][Zz][Yy]\s*\d+[-–]\d+)",
        r"([Dd][Ss][Yy]\s*\d+[-–]\d+)",
        r"([Dd][Xx][Yy]\s*\d+[-–]\d+)",
        r"([Dd][Zz][Yy]\s*\d+)",
        r"([Dd][Ss][Yy]\s*\d+)",
        r"([Dd][Xx][Yy]\s*\d+)",
        # Bare pinyin: SY1-100 / ZY1-200 / XY1-100
        r"([Ss][Yy]\s*\d+[-–]\d+)",
        r"([Zz][Yy]\s*\d+[-–]\d+)",
        r"([Xx][Yy]\s*\d+[-–]\d+)",
        # Bare pinyin single number: ZY103 / SY201
        r"([Ss][Yy]\s*\d+)",
        r"([Zz][Yy]\s*\d+)",
        r"([Xx][Yy]\s*\d+)",
        # Generic classroom/location patterns
        r"(教室[：:]\s*\S+)",
        r"(地点[：:]\s*\S+)",
        r"([Ll]ocation[：:]\s*.+)",
        r"([Rr]oom\s+[A-Za-z0-9\-]+)",
        r"(教学楼\s*\S+)",
        r"(\S+教室)",
        # SJTU landmark buildings
        r"(陈瑞球楼\s*\S*)",
        r"(包玉刚图书馆\s*\S*)",
        r"(李政道图书馆\s*\S*)",
        r"(龙宾楼\s*\S*)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return ""


def search_events_by_keywords(text, config, course_name):
    """Search text for OH/RC/Exam keywords and parse associated times/locations"""
    found = []
    lines = text.split("\n")
    all_kw = (config.get("oh_keywords", [])
              + config.get("rc_keywords", [])
              + config.get("exam_keywords", []))

    for i, line in enumerate(lines):
        matched = next((kw for kw in all_kw if _keyword_match(kw, line)), None)
        if not matched:
            continue
        is_oh = any(_keyword_match(kw, line) for kw in config.get("oh_keywords", []))
        is_rc = any(_keyword_match(kw, line) for kw in config.get("rc_keywords", []))
        is_exam = any(_keyword_match(kw, line) for kw in config.get("exam_keywords", []))
        # Priority: Exam > OH > RC
        if is_exam:
            etype = "Exam"
        elif is_oh:
            etype = "OH"
        else:
            etype = "RC"
        # Wider context window to capture location info
        ctx_start = max(0, i - 3)
        ctx_end = min(len(lines), i + 4)
        context = "\n".join(lines[ctx_start:ctx_end])
        location = extract_location(context)

        # Absolute date patterns (e.g. "Wednesday, 27th May, 20:20-21:50")
        for t in parse_absolute_time(context):
            found.append({
                "type": etype,
                "title": f"[{etype}] {course_name}",
                "start": t["start_datetime"],
                "end": t["end_datetime"],
                "day_of_week": t["start_datetime"].weekday(),
                "is_absolute": True,
                "raw": line.strip(),
                "course_name": course_name,
                "location": location,
            })

        # Recurring weekday patterns (e.g. "Wednesday 14:00-16:00") — exams are never recurring
        if etype != "Exam":
            for t in parse_weekday_time(context):
                start_date = find_next_weekday(t["day_of_week"])
                found.append({
                    "type": etype,
                    "title": f"[{etype}] {course_name}",
                    "start": start_date.replace(hour=t["start_h"], minute=t["start_m"], second=0),
                    "end": start_date.replace(hour=t["end_h"], minute=t["end_m"], second=0),
                    "day_of_week": t["day_of_week"],
                    "is_absolute": False,
                    "raw": line.strip(),
                "course_name": course_name,
                "location": location,
            })
    return found


# ==============================
# Apple Calendar integration
# ==============================

def run_applescript(script, timeout=60):
    """Run an AppleScript snippet with a generous timeout.

    Returns (success: bool, stdout: str, stderr: str)."""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        print(f"  [Cal Error] {r.stderr.strip()[:120]}")
        return False, r.stdout, r.stderr
    return True, r.stdout, r.stderr


def _calendar_ensure_running():
    """One-shot attempt to bring Calendar.app to the foreground so AppleEvents work.

    Uses osascript 'activate' (more reliable from launchd than open -g).
    On failure, subsequent AppleScript calls will still try 'launch' as fallback.
    """
    import time
    subprocess.run(
        ["osascript", "-e", 'tell application "Calendar" to activate'],
        capture_output=True, text=True, timeout=30,
    )
    # Brief pause after activate to let Calendar finish launching
    time.sleep(2)


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', s)
    return s.replace('\\', '\\\\').replace('"', '\\"')[:200]


def calendar_ensure_exists(cal_name, account=""):
    """Verify the target calendar exists. Exit gracefully if not (user must create
    it manually in Calendar.app first).

    Does NOT clear the calendar — we use smart diff-based sync instead.
    """
    if account:
        cal_path = f'calendar "{cal_name}" of account "{account}"'
    else:
        cal_path = f'calendar "{cal_name}"'

    check_script = f'''
    tell application "Calendar"
        launch
        delay 1
        try
            set targetCal to {cal_path}
            return "found"
        on error
            return "not_found"
        end try
    end tell
    '''
    ok, stdout, _ = run_applescript(check_script, timeout=30)
    if not ok or "not_found" in stdout:
        print(f"[Cal] Calendar \"{cal_name}\" not found")
        if account:
            print(f"[Cal] Account: {account}")
        print(f"[Cal] Please create it manually in Calendar.app first:")
        print(f"[Cal]   Open Calendar.app → File → New Calendar → choose account → name it \"{cal_name}\"")
        print(f"[Cal]   Then re-run: python3 sync.py")
        sys.exit(0)
    print(f"[Cal] Calendar \"{cal_name}\" found")


def get_calendar_events(cal_name, account=""):
    """Read ALL events currently in the target calendar via AppleScript.

    Returns a list of dicts, each with enough fields to compute a fingerprint:
      {title, start, end, has_recurrence, day_of_week, start_hour, start_minute}

    Returns an empty list on failure (non-fatal — we'll just add everything).
    """
    if account:
        cal_path = f'calendar "{cal_name}" of account "{account}"'
    else:
        cal_path = f'calendar "{cal_name}"'

    # Read summary, start date, end date, and recurrence for every event.
    # We build ISO-like date strings manually to avoid «class isot» chevron
    # characters which break when passed through Python subprocess to osascript.
    read_script = f'''
    tell application "Calendar"
        launch
        try
            set out to ""
            tell {cal_path}
                set evList to every event
                repeat with ev in evList
                    try
                        set evTitle to summary of ev
                        set evStart to start date of ev
                        set evEnd to end date of ev
                        set evRecurrence to recurrence of ev
                        -- Build ISO-like: YYYY-MM-DDTHH:MM:SS (no chevrons)
                        set startISO to (year of evStart as integer as string) & "-" & my pad2(month of evStart as integer) & "-" & my pad2(day of evStart) & "T" & my pad2(hours of evStart) & ":" & my pad2(minutes of evStart) & ":00"
                        set endISO to (year of evEnd as integer as string) & "-" & my pad2(month of evEnd as integer) & "-" & my pad2(day of evEnd) & "T" & my pad2(hours of evEnd) & ":" & my pad2(minutes of evEnd) & ":00"
                        set out to out & evTitle & "|||" & startISO & "|||" & endISO & "|||" & evRecurrence & return
                    end try
                end repeat
            end tell
            return out
        on error errMsg
            return "ERROR: " & errMsg
        end try
    end tell
    on pad2(n)
        if n < 10 then
            return "0" & n
        end if
        return n as string
    end pad2
    '''
    ok, stdout, _ = run_applescript(read_script, timeout=120)
    if not ok or stdout.startswith("ERROR:"):
        print(f"[Cal] WARNING: Could not read existing events (non-fatal): {stdout[:200]}")
        return []

    events = []
    for line in stdout.strip().split("\n"):
        if not line or "|||" not in line:
            continue
        parts = line.split("|||")
        if len(parts) < 4:
            continue
        title = parts[0].strip()
        start_str = parts[1].strip()
        end_str = parts[2].strip()
        recurrence_str = parts[3].strip() if len(parts) > 3 else ""

        has_recurrence = bool(recurrence_str and recurrence_str != "missing value")

        # Parse ISO date formats from AppleScript: "2026-06-19T16:00:00" or "2026-06-19T16:00:00+0800"
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

        # Ignore timezone offset for local comparison
        if start_dt.tzinfo is not None:
            start_dt = start_dt.replace(tzinfo=None)
        if end_dt.tzinfo is not None:
            end_dt = end_dt.replace(tzinfo=None)

        events.append({
            "title": title,
            "start": start_dt,
            "end": end_dt,
            "has_recurrence": has_recurrence,
            "day_of_week": start_dt.weekday(),
            "start_hour": start_dt.hour,
            "start_minute": start_dt.minute,
        })

    print(f"[Cal] Read {len(events)} existing events from \"{cal_name}\"")
    return events


def event_fingerprint(e):
    """Generate a unique string key for matching/merging calendar events.

    For recurring events:  "{title}|weekly|{dow}|{hour}:{minute}"
    For one-time events:   "{title}|{start:%Y-%m-%d %H:%M}|{end:%Y-%m-%d %H:%M}"
    """
    if e.get("has_recurrence") or not e.get("is_absolute", True):
        dow = e.get("day_of_week", 0)
        sh = e.get("start_hour", e["start"].hour)
        sm = e.get("start_minute", e["start"].minute)
        return f"{e['title']}|weekly|{dow}|{sh:02d}:{sm:02d}"
    else:
        return f"{e['title']}|{e['start']:%Y-%m-%d %H:%M}|{e['end']:%Y-%m-%d %H:%M}"


WDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON = ["January", "February", "March", "April", "May", "June",
       "July", "August", "September", "October", "November", "December"]


def applescript_date(dt, with_time=False):
    """Convert datetime to AppleScript-compatible English date string"""
    wd = WDAY[dt.weekday()]
    mo = MON[dt.month - 1]
    if with_time:
        h12 = dt.hour % 12
        if h12 == 0:
            h12 = 12
        ampm = "AM" if dt.hour < 12 else "PM"
        return f"{wd}, {mo} {dt.day}, {dt.year} at {h12}:{dt.minute:02d}:00 {ampm}"
    return f"{wd}, {mo} {dt.day}, {dt.year}"


def calendar_add_event(cal_name, cal_account, summary, start_dt, end_dt,
                       desc="", location="", recurrence=""):
    """Add a single event via AppleScript. Returns True on success."""
    s = clean_text(summary)
    d = clean_text(desc)
    loc = clean_text(location)

    if cal_account:
        cal_path = f'calendar "{cal_name}" of account "{cal_account}"'
    else:
        cal_path = f'calendar "{cal_name}"'

    is_allday = (start_dt.hour == 0 and start_dt.minute == 0 and
                 end_dt.hour == 0 and end_dt.minute == 0)

    if is_allday:
        date_s = applescript_date(start_dt, with_time=False)
        props = f'summary:"{s}", start date:date "{date_s}", end date:date "{date_s}", allday event:true, description:"{d}"'
        if loc:
            props += f', location:"{loc}"'
        script = f'''
        tell application "Calendar"
            launch
            tell {cal_path}
                make new event with properties {{{props}}}
            end tell
        end tell
        '''
    else:
        start_str = applescript_date(start_dt, with_time=True)
        end_str = applescript_date(end_dt, with_time=True)
        props = f'summary:"{s}", start date:date "{start_str}", end date:date "{end_str}", description:"{d}"'
        if loc:
            props += f', location:"{loc}"'
        if recurrence:
            props += f', recurrence:"{recurrence}"'
        script = f'''
        tell application "Calendar"
            launch
            tell {cal_path}
                make new event with properties {{{props}}}
            end tell
        end tell
        '''

    ok, _, _ = run_applescript(script, timeout=60)
    return ok


def calendar_remove_event(cal_name, cal_account, title, start_dt, has_recurrence=False):
    """Remove a single matching event from the calendar via AppleScript.

    Matches by title AND full start datetime (not just date). For recurring
    events this targets the specific first-occurrence.

    NOTE: AppleScript CANNOT delete recurring events from Calendar.app
    (macOS limitation — deletion of recurring events is silently ignored).
    For recurring events, set has_recurrence=True to skip the removal attempt.
    """
    if has_recurrence:
        return False  # RFC: recurring events can't be deleted via AppleScript

    s = clean_text(title)
    date_s = applescript_date(start_dt, with_time=True)

    if cal_account:
        cal_path = f'calendar "{cal_name}" of account "{cal_account}"'
    else:
        cal_path = f'calendar "{cal_name}"'

    script = f'''
    tell application "Calendar"
        launch
        tell {cal_path}
            set evList to (every event whose summary is "{s}" and start date = date "{date_s}")
            repeat with ev in evList
                try
                    delete ev
                end try
            end repeat
        end tell
    end tell
    '''
    ok, _, _ = run_applescript(script, timeout=60)
    return ok


# ==============================
# Main
# ==============================

def main():
    print("=" * 50)
    print(f"Canvas → Calendar  |  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    config = load_config()
    cal_name = config["sync"]["calendar_name"]
    cal_account = config["sync"].get("calendar_account", "")

    # 1. Fetch data from Canvas
    courses = get_courses()

    # Course filter
    cf = config.get("course_filter", {})
    if cf.get("list"):
        mode = cf.get("mode", "exclude")
        filtered = []
        for c in courses:
            matched = any(kw.lower() in c["name"].lower() for kw in cf["list"])
            if (mode == "include" and matched) or (mode == "exclude" and not matched):
                filtered.append(c)
            else:
                print(f"[Filter] {'Include' if mode == 'include' else 'Exclude'}: {c['name']}")
        courses = filtered

    all_assignments = []
    all_events = []
    cancel_list = []

    for course in courses:
        assignments = get_assignments(course)
        all_assignments.extend(assignments)
        print(f"[API] {course['name']}: {len(assignments)} assignments")

    # --- Event extraction (OH/RC/Exam): LLM first, fall back to regex ---
    try:
        llm_events, cancels = extract_events_llm(config, courses)
        if llm_events is not None:
            all_events = llm_events
            cancel_list = cancels
        else:
            raise Exception("LLM extraction failed")
    except Exception as e:
        print(f"[LLM] Falling back to regex extraction ({e})")
        # Regex fallback: per-course extraction
        for course in courses:
            syllabus = get_syllabus_text(course)
            if syllabus:
                items = search_events_by_keywords(syllabus, config, course["name"])
                all_events.extend(items)

            pages = get_page_texts(course)
            for p in pages:
                items = search_events_by_keywords(p["text"], config, course["name"])
                all_events.extend(items)

            announcements = get_announcements(course)
            for ann in announcements:
                items = search_events_by_keywords(ann["text"], config, course["name"])
                all_events.extend(items)

    print(f"\n[Result] {len(all_assignments)} assignments, {len(all_events)} events"
          + (f", {len(cancel_list)} to cancel" if cancel_list else ""))

    # 2. Sync to Apple Calendar (smart diff — no destructive clear-all)
    _calendar_ensure_running()
    calendar_ensure_exists(cal_name, cal_account)

    # Read what's currently in the calendar so we can avoid duplicates
    existing_events = get_calendar_events(cal_name, cal_account)
    existing_fps = set()
    for ev in existing_events:
        existing_fps.add(event_fingerprint(ev))

    # Build desired event list with fingerprints (assignments + OH/RC/Exam)
    desired = []  # list of (fingerprint, event_dict, add_kwargs)
    desired_fps = set()

    # -- Assignments --
    for a in all_assignments:
        end_dt = a["due_date"]
        start_dt = end_dt - timedelta(hours=1)
        title = f"Due: {a['title']} ({a['due_date']:%H:%M})"
        desc = f"Course: {a['course_name']}\\nDue: {a['due_date']:%Y-%m-%d %H:%M}\\n{a['url']}"
        ev = {
            "title": title,
            "start": start_dt,
            "end": end_dt,
            "is_absolute": True,
            "has_recurrence": False,
            "day_of_week": start_dt.weekday(),
            "start_hour": start_dt.hour,
            "start_minute": start_dt.minute,
        }
        fp = event_fingerprint(ev)
        if fp not in desired_fps:
            desired_fps.add(fp)
            desired.append((fp, ev, {
                "summary": title, "start_dt": start_dt, "end_dt": end_dt,
                "desc": desc, "location": "", "recurrence": "",
            }))

    # -- OH/RC/Exam events (with dedup + cancellation filter) --
    protected_keys = set()
    for e in all_events:
        protected_keys.add((e["course_name"], e["type"], e["day_of_week"], e["start"].hour))

    seen_event_keys = set()
    for e in all_events:
        if e.get("is_absolute"):
            key = (e["course_name"], e["type"], "abs:" + e["start"].strftime("%Y-%m-%d %H"))
        else:
            key = (e["course_name"], e["type"], e["day_of_week"], e["start"].hour)
        if key in seen_event_keys:
            continue

        # Cancel check
        is_cancelled = False
        for ca in cancel_list:
            target_key = (ca["course_name"], ca["type"], ca["day_of_week"], ca["start_hour"])
            if target_key in protected_keys:
                continue
            if (ca["course_name"] == e["course_name"]
                and ca["type"] == e["type"]
                and ca["day_of_week"] == e["day_of_week"]):
                if ca["start_hour"] is None or ca["start_hour"] == e["start"].hour:
                    is_cancelled = True
                    print(f"  [Cancel] Skipping {e['title']} (weekday={e['day_of_week']}, hour={e['start'].hour})")
                    break
        if is_cancelled:
            continue

        seen_event_keys.add(key)
        desc = f"Source: {e['raw']}"
        loc = e.get("location", "")
        if loc:
            desc += f"\\nLocation: {loc}"
        rec = "" if e.get("is_absolute") else "FREQ=WEEKLY;INTERVAL=1"

        ev = {
            "title": e["title"],
            "start": e["start"],
            "end": e["end"],
            "is_absolute": e.get("is_absolute", False),
            "has_recurrence": bool(rec),
            "day_of_week": e["day_of_week"],
            "start_hour": e["start"].hour,
            "start_minute": e["start"].minute,
        }
        fp = event_fingerprint(ev)
        if fp not in desired_fps:
            desired_fps.add(fp)
            desired.append((fp, ev, {
                "summary": e["title"], "start_dt": e["start"], "end_dt": e["end"],
                "desc": desc, "location": loc, "recurrence": rec,
            }))

    print(f"[Sync] Desired: {len(desired)} events, Existing in Calendar: {len(existing_fps)} events")

    # --- Diff and apply ---
    # 1. Add events in desired but not in existing
    ok = fail = 0
    added = []
    new_fps = set()

    for fp, ev, kwargs in desired:
        if fp in existing_fps:
            # Already in calendar — skip (no duplicate)
            continue
        if fp in new_fps:
            continue  # already added this batch
        if calendar_add_event(cal_name, cal_account, **kwargs):
            ok += 1
            new_fps.add(fp)
            added.append(f"{ev.get('type', 'Assignment'):5s} | {ev['title']} | {ev['start']:%Y-%m-%d %a %H:%M}")
        else:
            fail += 1

    # 2. Remove stale events AND deduplicate (keep only one copy per fingerprint).
    # Track (title, start_dt) pairs already targeted for removal — since
    # calendar_remove_event deletes ALL events matching that pair, we only need
    # to call it once per unique pair.
    removed = 0
    removed_dup = 0
    removed_pairs = set()  # (title, start_dt_str) already removed
    kept_fps = set()

    for ev in existing_events:
        fp = event_fingerprint(ev)
        pair_key = (ev["title"], ev["start"].strftime("%Y-%m-%d %H:%M"))

        if pair_key in removed_pairs:
            continue  # already removed by a previous call

        if fp not in desired_fps:
            # Stale: no longer in the desired set
            if calendar_remove_event(cal_name, cal_account, ev["title"], ev["start"],
                                     has_recurrence=ev.get("has_recurrence", False)):
                removed += 1
                removed_pairs.add(pair_key)
                print(f"  [Remove stale] {ev['title']} ({ev['start']:%Y-%m-%d %H:%M})")
        elif fp in kept_fps:
            # Duplicate: fingerprint is desired but we already kept one copy
            if calendar_remove_event(cal_name, cal_account, ev["title"], ev["start"],
                                     has_recurrence=ev.get("has_recurrence", False)):
                removed_dup += 1
                removed_pairs.add(pair_key)
                print(f"  [Remove dup] {ev['title']} ({ev['start']:%Y-%m-%d %H:%M})")
        else:
            # First occurrence of this fingerprint — keep it
            kept_fps.add(fp)

    if removed or removed_dup:
        print(f"[Sync] Removed {removed} stale + {removed_dup} duplicate events")

    # Write summary file so user can review what was added
    SUMMARY_PATH = DATA_DIR / "last_summary.txt"
    with open(SUMMARY_PATH, "w") as f:
        f.write(f"Canvas Sync — {datetime.now():%Y-%m-%d %H:%M}\n")
        f.write(f"{'='*60}\n")
        for line in added:
            f.write(line + "\n")
        if removed or removed_dup:
            f.write(f"{'='*60}\n")
            f.write(f"Removed {removed} stale + {removed_dup} duplicate events\n")
        f.write(f"{'='*60}\n")
        f.write(f"Added: {ok}, Failed: {fail}, Removed: {removed} stale + {removed_dup} dup\n")

    total_removed = removed + removed_dup
    print(f"[Cal] {ok} added" + (f", {fail} failed" if fail else "") +
          (f", {total_removed} removed" if total_removed else ""))

    # Save sync record with fingerprints for future runs
    with open(LAST_SYNC_PATH, "w") as f:
        json.dump({
            "last_sync": datetime.now().isoformat(),
            "assignments": len(all_assignments),
            "events_desired": len(desired),
            "events_added": ok,
            "events_removed": removed,
            "events_duplicates_removed": removed_dup,
            "events_failed": fail,
        }, f, indent=2)

    # Email summary if configured
    email = config.get("sync", {}).get("email", "")
    if email:
        summary_path_abs = str(SUMMARY_PATH.resolve())
        _, _, _ = run_applescript(f'''
        set summaryFile to POSIX file "{summary_path_abs}"
        set bodyText to read summaryFile as «class utf8»
        tell application "Mail"
            set outMsg to make new outgoing message with properties {{subject:"Canvas Sync — {datetime.now():%Y-%m-%d %H:%M}", content:bodyText, visible:false}}
            tell outMsg
                make new to recipient at end of to recipients with properties {{address:"{email}"}}
            end tell
            send outMsg
        end tell
        ''')
        print(f"[Mail] Summary sent to {email}")
    print("[Done] Sync complete")


if __name__ == "__main__":
    main()
