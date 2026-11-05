# Meeting Pipeline

Turns a meeting recording into a speaker-separated transcript and a numbered list of action items. If you give it your own meeting notes, it also checks them against the recording.

## Setup

1. Install Python 3.9+ and [ffmpeg](https://ffmpeg.org/download.html) (it must be on `PATH`).
2. Install the packages:
   ```bash
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and add your OpenAI API key:
   ```
   OPENAI_API_KEY=sk-...
   ```
   Your OpenAI project needs access to `gpt-4o-transcribe-diarize` and `gpt-5.6-luna`. You can change the models at the top of `meeting_pipeline.py`.

## Usage

**Easiest:** open `run_meeting.py`, set `AUDIO_FILE` (and `NOTES_FILE` if you have notes), then run:
```bash
python run_meeting.py
```
On Windows you can also double-click `Run Meeting.bat`, or drag a recording onto it.

**Command line:**
```bash
python meeting_pipeline.py "recording.aac"
python meeting_pipeline.py "recording.aac" --notes "my_notes.txt" --title "Weekly review" --date "22 Oct 2026"
```

**Many recordings:** put them in `meetings/inbox/` and run:
```bash
python meeting_pipeline.py --inbox
```
A file named `<recording name>_notes.txt` next to a recording is used automatically.

## Output

Results go to `meetings/<recording name>/`:

- `transcript.txt`: the transcript, with speakers and timestamps
- `action_items.txt`: numbered action points, ready to paste into a task tool
- `review.txt`: corrections to your notes, points to confirm, and evidence for each action point
- `coverage_report.txt`: shows that no part of the audio was skipped

Re-running the same recording reuses the saved transcript, so only the action-item step runs again. The action list is an AI draft: check `review.txt` before sharing it.
