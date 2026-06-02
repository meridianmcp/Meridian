# Meridian Checkpoint — Chrome Extension

Auto-checkpoint your Meridian project when you type "checkpoint" in claude.ai, or on a timer.

## Install (load unpacked)

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `extensions/` folder from this repo

## Configure

Click the Meridian icon in your toolbar:
- **Meridian server URL** — defaults to `http://localhost:7878` for local self-hosted
- **Project ID** — your Meridian project UUID (find it in the dashboard)
- **Trigger** — fires checkpoint when the word "checkpoint" appears in your claude.ai message
- **Timer** — optionally auto-checkpoint every N minutes

## What it does

When triggered (keyword or timer), the extension POSTs to `POST /hooks/stop` on your
Meridian server with your project ID. This runs `auto_capture` (buckets done tasks
into a note) and generates a delta handoff file so your next session can resume quickly.

## Package as zip (for distribution)

```bash
cd extensions/
zip -r meridian-checkpoint.zip . --exclude="*.md" --exclude=".DS_Store"
```

## Trigger phrases

- `checkpoint`
- `save progress`
- `/meridian`

## Notes

- The extension only requests permissions for `claude.ai`, `localhost`, and `usemeridian.us`
- No data is sent to any third party — checkpoint fires directly to your Meridian server
- Works with both self-hosted and usemeridian.us hosted tier
