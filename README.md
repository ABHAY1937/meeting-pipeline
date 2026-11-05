# Meeting Pipeline

Turn a meeting recording into a **speaker-separated transcript**, a **numbered action-item list** ready for Teamwork (or any task tool), and a **review file** that cross-checks your own meeting notes against what was actually said.

```
recording (.aac/.mp3/.mp4/...) ──► transcript.txt ──► action_items.txt
                                         │                 review.txt
            your notes (optional) ───────┘           coverage_report.txt
```

## Features

- **Any audio or video format** that ffmpeg can read (`.aac`, `.mp3`, `.m4a`, `.wav`, `.mp4`, `.mov`, `.webm`, …).
- **Long recordings:** audio is split into ~10-minute chunks, cut at natural pauses so no word is split.
- **Speaker separation** (diarization) that keeps the same `Speaker N` label across chunks by passing voice samples forward.
- **No audio skipped:** non-silent gaps are re-transcribed and marked *"recovered from faint audio"*. A coverage report proves full coverage.
- **Action items** grouped by topic, with exact figures and names. Uncertain points are flagged for confirmation instead of guessed.
- **Notes cross-check:** each of your notes is confirmed, corrected (with timestamped evidence), flagged as not in the recording, or removed with a reason. No note is ever silently dropped.
- **Cheap re-runs:** transcripts are cached, so re-running with new notes only repeats the action-item step.
- **Inbox mode:** drop recordings into `meetings/inbox/` and process them all in one command.

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.9+ | `python --version` |
| ffmpeg + ffprobe on `PATH` | Windows: `winget install Gyan.FFmpeg`; macOS: `brew install ffmpeg`; Linux: `apt install ffmpeg` |
| OpenAI API key | The project needs access to the models below |
| Models | `gpt-4o-transcribe-diarize` (transcription), `gpt-5.6-luna` (analysis). Both can be changed at the top of `meeting_pipeline.py` |

## Setup

```bash
git clone https://github.com/ABHAY1937/meeting-pipeline.git
cd meeting-pipeline

python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # Windows: copy .env.example .env
# then edit .env and paste your key:  OPENAI_API_KEY=sk-...
```

The `.env` file is git-ignored, so your key is never committed.

## Usage

### Option 1: edit and run (easiest)

1. Open `run_meeting.py` and set `AUDIO_FILE` (and optionally `NOTES_FILE`, `TITLE`, `DATE`).
2. Run it:
   ```bash
   python run_meeting.py
   ```
   On Windows you can also double-click **`Run Meeting.bat`**.

### Option 2: drag and drop (Windows)

Drag a recording onto **`Run Meeting.bat`**. If a file named `<recording name>_notes.txt` sits next to the recording, it is picked up automatically.

### Option 3: command line

```bash
# transcript + action items
python meeting_pipeline.py "Thursday at 15-10.aac"

# also cross-check your own notes (plain .txt, any bullet format)
python meeting_pipeline.py "Thursday at 15-10.aac" --notes "my_notes.txt"

# optional title and date shown at the top of the action list
python meeting_pipeline.py "recording.mp4" --title "Management meeting 2" --date "22 Oct 2026"
```

### Option 4: inbox mode (many recordings)

Put recordings (and any matching `<name>_notes.txt` files) in `meetings/inbox/`, then run:

```bash
python meeting_pipeline.py --inbox
```

Recordings that are already processed are skipped, unless their notes file is newer than the last result.

## Output

Results are written to `meetings/<recording name>/`:

| File | Contents |
|---|---|
| `transcript.txt` | Speaker-separated transcript with `[hh:mm:ss]` timestamps |
| `coverage_report.txt` | Duration, chunks, words, speakers, gaps re-checked, recovered lines, noise-only stretches |
| `action_items.txt` | Clean numbered action list grouped by topic, ready to paste into Teamwork |
| `review.txt` | Corrections to your notes, removed items, points to confirm, and evidence for every action point |
| `notes_used.txt` | Copy of the notes used for the cross-check (if any) |
| `work/` | Cache (chunk transcripts, speaker samples, raw analysis JSON). Delete it to force a full re-run |

> The action list is an AI draft. Always check `review.txt` before sharing or creating tasks.

## How it works

1. **Split:** `ffprobe` reads the duration and `ffmpeg silencedetect` finds pauses. Cut points are placed at the longest pause near each 10-minute mark.
2. **Transcribe:** each chunk goes to `gpt-4o-transcribe-diarize`. Speaker labels are mapped to global `Speaker N` names, and short voice samples (up to 4 speakers) are sent with later chunks so labels stay consistent.
3. **Coverage check:** untranscribed stretches longer than 4 s that are not mostly silence are re-transcribed. New text is added and marked as recovered; fillers and duplicates are discarded.
4. **Analyse:** the transcript (plus notes) goes to the analysis model with strict rules: no invented facts, exact figures with currency, uncertain items flagged. A safeguard re-adds any note line the model did not account for.

Tuning constants (chunk length, silence threshold, gap length, models) are at the top of `meeting_pipeline.py`.

## Project structure

```
meeting-pipeline/
├── meeting_pipeline.py   # the pipeline (CLI)
├── run_meeting.py        # edit-and-run entry point
├── Run Meeting.bat       # Windows launcher (double-click or drag & drop)
├── requirements.txt
├── .env.example          # copy to .env and add your key
└── meetings/             # results (git-ignored, folder kept)
    └── inbox/            # drop recordings here for --inbox mode
```

## Privacy

Recordings, transcripts and results stay in `meetings/`, which is git-ignored. Only the folder structure is kept in the repo. Audio is sent to the OpenAI API for transcription and analysis, so make sure you have consent to record and process the meeting.

## Troubleshooting

| Problem | Fix |
|---|---|
| `OPENAI_API_KEY not found` | Create `.env` next to `meeting_pipeline.py` with `OPENAI_API_KEY=...` |
| `FileNotFoundError: ffmpeg` / `ffprobe` | Install ffmpeg and make sure it is on `PATH` (`ffmpeg -version`) |
| Model not found / access denied | Your OpenAI project needs access to both models, or change `TRANSCRIBE_MODEL` / `ANALYSIS_MODEL` |
| Garbled characters in the Windows console | Use `Run Meeting.bat` (it sets `PYTHONIOENCODING=utf-8`) or run `set PYTHONIOENCODING=utf-8` first |
| Want a fresh transcript | Delete `meetings/<recording name>/work/` and run again |
