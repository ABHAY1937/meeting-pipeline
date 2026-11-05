"""
RUN A MEETING THROUGH THE FULL PIPELINE
Change only the settings below, save, then run:   python run_meeting.py
(or double-click "Run Meeting.bat")
"""

# ======================= CHANGE ONLY THESE =======================

# Recording - any audio or video format (.aac, .mp3, .m4a, .wav, .mp4 ...)
AUDIO_FILE = r"C:\path\to\recording.aac"
# Your own notes to cross-check (a .txt file). Leave as "" if you have none.
NOTES_FILE = r""

# Optional - shown at the top of the action list. Leave as "" to let the AI name it.
TITLE = ""
DATE = ""          # e.g. "22 Oct 2026"

# =================================================================

from meeting_pipeline import process

process(AUDIO_FILE, NOTES_FILE or None, TITLE or None, DATE or None)
