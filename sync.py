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

import requests

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
#  Canvas API data fetching
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
#  Time parsing
# ==============================

def find_next_weekday(target_weekday, after=None):
    after = after or datetime.now()
    days_ahead = target_weekday - after.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return after + timedelta(days=days_ahead)


def parse_weekday_time(text):
    """Parse weekday + time patterns like 'Wednesday 14:00-16:00' or '周三 14:00'"""
    results = []
    # Chinese: 星期三 14:00-16:00 / 每周三 10:00
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


def extract_location(text):
    """Extract room/building location from text (supports SJTU naming conventions)"""
    patterns = [
        # Full Chinese names
        r"(东[上下中]院\s*\d+[-–]\d+)",        # 东中院1-200
        r"([上下中]院\s*\d+[-–]\d+)",           # 上院1-100
        r"(东[上下中]院\s*\d+)",                # 东中院201
        r"([上下中]院\s*\d+)",                  # 上院105
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


def search_oh_rc(text, config, course_name):
    """Search text for OH/RC keywords and parse associated times/locations"""
    found = []
    lines = text.split("\n")
    all_kw = config["oh_keywords"] + config["rc_keywords"]

    for i, line in enumerate(lines):
        matched = next((kw for kw in all_kw if kw.lower() in line.lower()), None)
        if not matched:
            continue
        # Wider context window to capture location info
        ctx_start = max(0, i - 3)
        ctx_end = min(len(lines), i + 4)
        context = "\n".join(lines[ctx_start:ctx_end])
        times = parse_weekday_time(context)

        for t in times:
            is_oh = any(kw.lower() in line.lower() for kw in config["oh_keywords"])
            etype = "OH" if is_oh else "RC"
            start_date = find_next_weekday(t["day_of_week"])
            location = extract_location(context)
            found.append({
                "type": etype,
                "title": f"[{etype}] {course_name}",
                "start": start_date.replace(hour=t["start_h"], minute=t["start_m"], second=0),
                "end": start_date.replace(hour=t["end_h"], minute=t["end_m"], second=0),
                "day_of_week": t["day_of_week"],
                "raw": line.strip(),
                "course_name": course_name,
                "location": location,
            })
    return found


# ==============================
#  Apple Calendar integration
# ==============================

def run_applescript(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"  [Cal Error] {r.stderr.strip()[:120]}")
        return False
    return True


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', '', s)
    return s.replace('\\', '\\\\').replace('"', '\\"')[:200]


def ensure_calendar(name):
    script = f'''
    tell application "Calendar"
        launch
        delay 1
        try
            set targetCal to first calendar whose name is "{name}"
        on error
            make new calendar with properties {{name:"{name}"}}
            set targetCal to first calendar whose name is "{name}"
        end try
        tell targetCal
            delete every event
        end tell
    end tell
    '''
    if run_applescript(script):
        print(f"[Cal] Cleared calendar \"{name}\"")


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


def add_event(cal_name, summary, start_dt, end_dt, desc="", location=""):
    s = clean_text(summary)
    d = clean_text(desc)
    loc = clean_text(location)

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
            try
                set targetCal to first calendar whose name is "{cal_name}"
            end try
            tell targetCal
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
        script = f'''
        tell application "Calendar"
            launch
            try
                set targetCal to first calendar whose name is "{cal_name}"
            end try
            tell targetCal
                make new event with properties {{{props}}}
            end tell
        end tell
        '''

    return run_applescript(script)


# ==============================
#  Main
# ==============================

def main():
    print("=" * 50)
    print(f"Canvas → Calendar  |  {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 50)

    config = load_config()
    cal_name = config["sync"]["calendar_name"]

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
    all_oh_rc = []

    for course in courses:
        # Assignments
        assignments = get_assignments(course)
        all_assignments.extend(assignments)
        print(f"[API] {course['name']}: {len(assignments)} assignments")

        # OH/RC from syllabus
        syllabus = get_syllabus_text(course)
        if syllabus:
            items = search_oh_rc(syllabus, config, course["name"])
            all_oh_rc.extend(items)

        # OH/RC from pages
        pages = get_page_texts(course)
        for p in pages:
            items = search_oh_rc(p["text"], config, course["name"])
            all_oh_rc.extend(items)

    print(f"\n[Result] {len(all_assignments)} assignments, {len(all_oh_rc)} OH/RC")

    # 2. Write to Apple Calendar
    ensure_calendar(cal_name)

    ok = fail = 0
    for a in all_assignments:
        desc = f"Course: {a['course_name']}\\nDue: {a['due_date']:%Y-%m-%d %H:%M}\\n{a['url']}"
        end_dt = a["due_date"]
        start_dt = end_dt - timedelta(minutes=30)
        if add_event(cal_name, f"[Assignment] {a['title']}", start_dt, end_dt, desc):
            ok += 1
        else:
            fail += 1

    # OH/RC dedup
    seen = set()
    for e in all_oh_rc:
        key = (e["course_name"], e["type"], e["day_of_week"], e["start"].hour)
        if key in seen:
            continue
        seen.add(key)
        desc = f"Source: {e['raw']}"
        loc = e.get("location", "")
        if loc:
            desc += f"\\nLocation: {loc}"
        if add_event(cal_name, e["title"], e["start"], e["end"], desc, location=loc):
            ok += 1
        else:
            fail += 1

    print(f"[Cal] {ok} succeeded" + (f", {fail} failed" if fail else ""))

    # Save sync record
    with open(LAST_SYNC_PATH, "w") as f:
        json.dump({
            "last_sync": datetime.now().isoformat(),
            "assignments": len(all_assignments),
            "oh_rc": len(all_oh_rc),
            "events_added": ok,
        }, f, indent=2)

    print("[Done] Sync complete")


if __name__ == "__main__":
    main()
