# Canvas → Apple Calendar Sync

Automatically sync assignments, recitation classes (RC), office hours (OH), and exams from [SJTU Canvas](https://oc.sjtu.edu.cn) to macOS Apple Calendar — with LLM-powered natural language understanding for both English and Chinese announcements.

## Features

- **Assignment sync**: Fetches all assignments with due dates and creates calendar events
- **Intelligent RC/OH/Exam extraction**: Uses DeepSeek LLM to understand time, location, and schedule changes from announcements, syllabi, and pages
- **Exam room assignment**: Detects student-ID-based room assignments from announcements (e.g., "odd IDs → Room A, even → Room B")
- **Schedule change detection**: When an RC/OH is shifted (e.g., "moved from Monday to Wednesday"), automatically removes the old time and adds the new one
- **Automatic background sync**: Runs every 2 days via launchd, no manual work needed
- **SJTU location recognition**: Understands campus room naming (DZY, ZY103, 东中院, etc.)

## Prerequisites

You'll need two things. Setup takes about 5 minutes.

- **Canvas API Token** (generated from the Canvas website)
- **DeepSeek API Key** (from the DeepSeek website, used for intelligent RC/OH parsing)

---

## Step 1: Get a Canvas API Token

1. Open your browser and log in to [oc.sjtu.edu.cn](https://oc.sjtu.edu.cn)
2. Click your avatar (top-left) → **Account** → **Settings**
3. Scroll down to **Approved Integrations** → click **+ New Access Token**
4. Set purpose to `Canvas Calendar Sync`, expiration to **No Expiration**
5. Click generate, then **copy the token and save it somewhere** — you won't be able to see it again after closing the page

---

## Step 2: Get a DeepSeek API Key

DeepSeek is a Chinese LLM provider that costs almost nothing. It reads your course announcements and understands human language like "RC shifted from Monday to Wednesday."

1. Go to [platform.deepseek.com](https://platform.deepseek.com)
2. Sign up (phone number is fine)
3. Go to **API Keys** → **Create API Key**
4. Copy the key (it starts with `sk-`)
5. **Top up 1 RMB** (that's all you'll need — each sync costs about 0.002 RMB)

---

## Step 3: Download the project

Open **Terminal** on your Mac (find it in Launchpad or search "Terminal"), then copy and paste:

```bash
git clone https://github.com/ReeseNoctis/canvas-cal-sync.git
cd canvas-cal-sync
```

---

## Step 4: Save your keys

In the same Terminal window (**replace the placeholder text with your actual keys**):

```bash
# Write your Canvas token (from Step 1)
echo "paste_your_canvas_token_here" > data/api_token.txt

# Write your DeepSeek API key (from Step 2, starts with sk-)
echo "paste_your_deepseek_key_here" > data/api_key_llm.txt
```

These files contain your personal keys and are ignored by git (listed in `.gitignore`).

---

## Step 5: Create the iCloud calendar

The script needs a calendar to write into. To sync events to your iPhone, create it under your **iCloud** account:

1. Open **Calendar.app** on your Mac
2. Click **File** → **New Calendar** → **iCloud** (if available; otherwise choose your iCloud account name)
3. Name it **SJTU Canvas**

> If you previously ran the script and have a local "SJTU Canvas" calendar, delete it first (right-click → Delete), then create the new one under iCloud.

## Step 6: Install and run

```bash
# Install dependencies
pip3 install --quiet requests openai

# Run once to test
python3 sync.py
```

Open **Calendar.app** — your **"SJTU Canvas"** calendar should be populated. Since it's an iCloud calendar, the events will appear on your iPhone automatically.

> If the calendar isn't found, the script will print exact instructions and exit.

---

## Step 7: Set up automatic sync (optional)

```bash
./setup.sh
```

This installs a launchd job that runs `sync.py` every 2 days in the background. No further action needed.

To stop auto-sync:

```bash
launchctl unload ~/Library/LaunchAgents/com.sjtu.canvassync.plist
```

---

## Configuration

Edit `config.json` to customize behavior:

### Course filter

```json
"course_filter": {
  "mode": "include",
  "list": [
    "ECE2160JSU2026",
    "GER1100JSU2026-1"
  ]
}
```

- `"mode": "include"` — only sync courses matching keywords in the list
- `"mode": "exclude"` — sync all courses except those matching keywords
- Matching is case-insensitive and partial (e.g., `"ECE2160"` matches `"ECE2160JSU2026"`)

### LLM settings

```json
"llm": {
  "provider": "deepseek",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat"
}
```

The default uses DeepSeek. To switch to another OpenAI-compatible provider, change `base_url` and `model`.

### OH/RC keywords

```json
"oh_keywords": ["Office Hour", "答疑", "OH:", "OH：", "Office Hours:"],
"rc_keywords": ["习题课", "Recitation", "RC:", "RC：", "习题", "RC"]
```

These keywords are used as a cheap pre-filter to find relevant text before sending it to the LLM. Add or remove keywords as needed.

### Exam keywords

```json
"exam_keywords": ["考试", "Exam", "midterm", "Final", "期中考试", "期末考试"]
```

Pre-filter keywords for exam detection in announcements, syllabi, and pages.

### Student ID (for exam room assignment)

```json
"student_id": "523XXXXXXXXX"
```

Fill in your student ID number. When exam announcements specify rooms by student ID ranges (e.g., "odd IDs → Room A, even → Room B"), the LLM uses your ID to determine the correct room. Leave empty if not needed.

### Calendar name

```json
"sync": {
  "calendar_name": "SJTU Canvas",
  "lookahead_days": 60
}
```

Change `calendar_name` to use a different calendar.

---

## FAQ

**Q: "API token not found" error?**
Make sure `data/api_token.txt` exists and contains your Canvas token.

**Q: "DeepSeek API key not found" error?**
Make sure `data/api_key_llm.txt` exists and the key starts with `sk-`.

**Q: Nothing appears in Calendar?**
1. Check that your filtered courses actually have content on Canvas
2. View the log: `cat data/sync.log`
3. Make sure the "SJTU Canvas" calendar isn't hidden in Calendar.app

**Q: How much does DeepSeek cost?**
About 0.002 RMB per sync (yes, that's 0.2 cents). At once every 2 days, that's roughly 0.03 RMB/month. A 1 RMB top-up lasts over 2 years.

**Q: Can I run it manually?**
```bash
cd canvas-cal-sync
python3 sync.py
```

**Q: It stopped working after a macOS update?**
Re-run the setup: `./setup.sh` (this re-registers the launchd job).

---

## How It Works

```
Canvas API                        DeepSeek LLM                Apple Calendar
    │                                  │                          │
    ├─ Fetch course list               │                          │
    ├─ Fetch assignments               │                          │
    ├─ Fetch announcements ──────────→ Extract RC/OH events:     │
    ├─ Fetch syllabus       ──────────→   Time, location,        │
    ├─ Fetch pages          ──────────→   Add/cancel/reschedule ─→ Write events
    │                                  │                          │
    │                           Semantic understanding:           │
    │                           "RC shifted from Mon to Wed"      │
    │                           → cancel Mon, add Wed             │
    │                           "8:20 PM" → 20:20                │
    │                           Filters noise: surveys, links     │
```

If the LLM call fails (network issue, quota, etc.), the script automatically falls back to regex-based extraction so sync never breaks.

---

## License

SJTU Global College
