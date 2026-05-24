# Canvas → Apple Calendar Sync

Sync assignments, recitation classes (RC), and office hours (OH) from [SJTU Canvas](https://oc.sjtu.edu.cn) to macOS Apple Calendar — automatically.

## How It Works

- Uses the Canvas REST API (no browser, no scraping)
- Fetches all active courses, assignments with due dates, and OH/RC info from syllabi and pages
- Writes events to a dedicated **"SJTU Canvas"** calendar in Apple Calendar
- Supports `launchd` for automatic background sync every 2 hours

## Quick Start

### 1. Get a Canvas API Token

1. Go to **oc.sjtu.edu.cn** → **Account** → **Settings**
2. Scroll to **Approved Integrations** → **+ New Access Token**
3. Set purpose to `Canvas Calendar Sync`, no expiration
4. Copy the generated token

### 2. Install

```bash
cd canvas-cal-sync

# Save your Canvas token
echo "YOUR_CANVAS_API_TOKEN" > data/api_token.txt

# Install dependencies & set up auto-sync
./setup.sh
```

### 3. Run

```bash
python3 sync.py
```

Open **Calendar.app** — you should see a new **"SJTU Canvas"** calendar populated with your assignments and any OH/RC events found.

## Configuration

Edit `config.json`:

### Course filter

Skip courses you don't want in your calendar:

```json
"course_filter": {
  "mode": "exclude",
  "list": ["TA Group", "TA Training", "Undergraduate Students"]
}
```

- `mode: "exclude"` — skip courses matching any keyword in `list`
- `mode: "include"` — only keep courses matching keywords
- Matching is case-insensitive and partial

### OH/RC keyword detection

The script scans course syllabi and pages for office hours and recitation info:

```json
"oh_keywords": ["Office Hour", "office hour", "答疑", "OH:", "OH：", "Office Hours:"],
"rc_keywords": ["习题课", "Recitation", "recitation", "RC:", "RC：", "习题"]
```

It parses time patterns in both Chinese (`星期三 14:00-16:00`, `每周三 10:00`) and English (`Monday 2:00 PM-4:00 PM`).

### Location extraction

Room/building names are automatically extracted from OH/RC context. Supported SJTU naming conventions:

| Style | Example |
|-------|---------|
| Full name | 东中院1-200, 上院105 |
| Chinese abbreviation | 东中1-200, 东上2-301 |
| Pinyin abbreviation | DZY1-200, DSY2-301, SY1-100 |
| No hyphen | DZY315 |
| Label-based | 教室: xxx, 地点: xxx, Location: Room 201 |

## Managing the Auto-Sync

```bash
# Start
launchctl load ~/Library/LaunchAgents/com.sjtu.canvassync.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.sjtu.canvassync.plist

# Check status
launchctl list | grep canvassync

# View logs
tail -f data/sync.log
```

The default interval is every 2 hours (7200 seconds). To change it, edit the `StartInterval` value in `com.sjtu.canvassync.plist` and reload.

## Event Format

- **Assignments**: 30-minute events ending at the due time, prefixed with `[Assignment]`
- **OH/RC**: All-day events extracted from syllabus/pages, with location and source text in the description

Each sync clears and rebuilds the **"SJTU Canvas"** calendar, so no duplicate events accumulate.

## Requirements

- macOS with Apple Calendar
- Python 3.9+
- `requests` library

## License

SJTU Global College
