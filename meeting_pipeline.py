"""
MEETING PIPELINE - audio/video -> speaker transcript -> action items -> cross-check your notes

USAGE
  1) Transcript + action items:
       python meeting_pipeline.py "Thursday at 15-10.aac"

  2) Same, plus cross-check your own notes (txt file, any format of bullet points):
       python meeting_pipeline.py "Thursday at 15-10.aac" --notes "my_notes.txt"
     (Re-running with notes is cheap: the transcript is cached and not paid for again.)

  3) Inbox mode - process every new recording dropped into meetings\\inbox:
       python meeting_pipeline.py --inbox
     A notes file named "<recording name>_notes.txt" next to the recording is picked up
     automatically. Re-runs only when there is a new recording or newer notes.

  Optional: --title "Management meeting 2"   --date "22 Oct 2026"

OUTPUT  (meetings\\<recording name>\\)
  transcript.txt        speaker-separated transcript with timestamps
  coverage_report.txt   proof that no audio was skipped
  action_items.txt      clean, numbered list - ready for Teamwork
  review.txt            what was corrected / added / removed / needs confirmation, with evidence

Needs: ffmpeg on PATH, .env next to this script containing OPENAI_API_KEY=...
"""
import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

HERE = Path(__file__).resolve().parent
MEETINGS_DIR = HERE / "meetings"
INBOX_DIR = MEETINGS_DIR / "inbox"

TRANSCRIBE_MODEL = "gpt-4o-transcribe-diarize"
ANALYSIS_MODEL = "gpt-5.6-luna"

MEDIA_EXTS = {".aac", ".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".wma", ".amr",
              ".webm", ".mp4", ".mov", ".mkv", ".avi", ".3gp", ".mpeg", ".mpga"}

TARGET_CHUNK_SECONDS = 10 * 60      # cut is moved to the nearest pause -> no word is split
CUT_SEARCH_WINDOW = 90
SILENCE_DB = "-35dB"
SILENCE_MIN = 0.4
GAP_FLAG_SECONDS = 4.0              # untranscribed, non-silent stretches longer than this are re-checked
MAX_KNOWN_SPEAKERS = 4              # API limit
FILLERS = {"okay", "ok", "yeah", "yes", "hmm", "mm", "mhmm", "uh", "um", "thank you", "thanks",
           "right", "no", "sure", "ah", "oh", "huh"}

key = dotenv_values(HERE / ".env").get("OPENAI_API_KEY")
if not key:
    sys.exit(f"OPENAI_API_KEY not found in {HERE / '.env'}")
client = OpenAI(api_key=key)


# ============================================================
# HELPERS
# ============================================================
def fmt(t):
    t = int(round(t))
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def log(msg=""):
    print(msg, flush=True)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-y", *args], check=True)


def media_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(out.strip())


def detect_silences(path):
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af",
         f"silencedetect=noise={SILENCE_DB}:d={SILENCE_MIN}", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    return list(zip(starts, ends))


def choose_cuts(duration, silences):
    cuts = [0.0]
    while duration - cuts[-1] > TARGET_CHUNK_SECONDS + CUT_SEARCH_WINDOW:
        target = cuts[-1] + TARGET_CHUNK_SECONDS
        near = [(s, e) for s, e in silences if abs((s + e) / 2 - target) <= CUT_SEARCH_WINDOW]
        if near:
            s, e = max(near, key=lambda se: (se[1] - se[0], -abs((se[0] + se[1]) / 2 - target)))
            cuts.append((s + e) / 2)
        else:
            cuts.append(target)
    cuts.append(duration)
    return cuts


def silent_fraction(start, end, silences):
    covered = sum(max(0.0, min(e, end) - max(s, start)) for s, e in silences)
    return covered / max(end - start, 1e-6)


def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def transcribe(path, known):
    kwargs = {}
    if known:
        kwargs = {"known_speaker_names": list(known), "known_speaker_references": list(known.values())}
    for attempt in range(1, 6):
        try:
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=TRANSCRIBE_MODEL, file=f, response_format="diarized_json",
                    chunking_strategy="auto", **kwargs)
            return [{"speaker": s.speaker, "start": s.start, "end": s.end, "text": s.text}
                    for s in resp.segments]
        except Exception as e:
            log(f"    attempt {attempt} failed: {str(e)[:160]}")
            if attempt == 5:
                raise
            time.sleep(15 * attempt)


# ============================================================
# STAGE 1 - TRANSCRIPT
# ============================================================
def build_transcript(media, out_dir):
    work = out_dir / "work"
    json_dir, ref_dir = work / "json", work / "refs"
    for d in (json_dir, ref_dir):
        d.mkdir(parents=True, exist_ok=True)

    log("[1/4] Splitting audio at pauses...")
    duration = media_duration(media)
    silences = detect_silences(media)
    cuts = choose_cuts(duration, silences)
    plan = work / "plan.json"
    if plan.exists() and json.loads(plan.read_text())["cuts"] != cuts:
        for f in json_dir.glob("*.json"):
            f.unlink()
    plan.write_text(json.dumps({"cuts": cuts}))
    log(f"      duration {fmt(duration)}, {len(cuts) - 1} chunk(s)")

    log("[2/4] Transcribing with speaker separation...")
    known, speaker_count, all_segments = {}, 0, []
    for i in range(len(cuts) - 1):
        start, end = cuts[i], cuts[i + 1]
        chunk = work / f"chunk_{i:03d}.mp3"
        cache = json_dir / f"chunk_{i:03d}.json"
        if cache.exists():
            segments = json.loads(cache.read_text(encoding="utf-8"))
            log(f"      chunk {i + 1}/{len(cuts) - 1}: cached")
        else:
            ffmpeg("-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(media), "-vn",
                   "-ac", "1", "-ar", "24000", "-b:a", "64k", str(chunk))
            t0 = time.time()
            segments = transcribe(chunk, known)
            cache.write_text(json.dumps(segments, ensure_ascii=False, indent=1), encoding="utf-8")
            log(f"      chunk {i + 1}/{len(cuts) - 1}: {len(segments)} segments in {time.time() - t0:.0f}s")

        # Map chunk labels to global "Speaker N" names and keep voice samples for later chunks
        mapping = {}
        for s in segments:
            label = s["speaker"]
            if label not in mapping:
                if label in known:
                    mapping[label] = label
                else:
                    speaker_count += 1
                    mapping[label] = f"Speaker {speaker_count}"
        for label, name in mapping.items():
            if name in known or len(known) >= MAX_KNOWN_SPEAKERS:
                continue
            segs = [s for s in segments if s["speaker"] == label]
            good = [s for s in segs if 2.0 <= s["end"] - s["start"] <= 10.0]
            pick = max(good or segs, key=lambda s: s["end"] - s["start"])
            if pick["end"] - pick["start"] < 2.0:
                continue
            ref = ref_dir / f"{name.replace(' ', '_')}.wav"
            if not ref.exists():
                if not chunk.exists():
                    ffmpeg("-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(media), "-vn",
                           "-ac", "1", "-ar", "24000", "-b:a", "64k", str(chunk))
                ffmpeg("-ss", f"{pick['start']:.3f}", "-to", f"{min(pick['end'], pick['start'] + 10):.3f}",
                       "-i", str(chunk), "-ac", "1", "-ar", "16000", str(ref))
            known[name] = "data:audio/wav;base64," + base64.b64encode(ref.read_bytes()).decode()

        for s in segments:
            if s["text"].strip():
                all_segments.append({"speaker": mapping[s["speaker"]], "start": start + s["start"],
                                     "end": start + s["end"], "text": s["text"].strip(), "recovered": False})
        chunk.unlink(missing_ok=True)

    log("[3/4] Checking coverage and recovering missed speech...")
    all_segments.sort(key=lambda s: s["start"])
    gaps, cursor = [], 0.0
    for s in all_segments + [{"start": duration, "end": duration}]:
        if s["start"] - cursor >= GAP_FLAG_SECONDS and silent_fraction(cursor, s["start"], silences) < 0.7:
            gaps.append((cursor, s["start"]))
        cursor = max(cursor, s["end"])

    gap_cache = json_dir / "gaps.json"
    recovered_all = json.loads(gap_cache.read_text(encoding="utf-8")) if gap_cache.exists() else {}
    noise_only = []
    for a, b in gaps:
        k = f"{a:.1f}-{b:.1f}"
        if k not in recovered_all:
            clip = work / "gap.mp3"
            ffmpeg("-ss", f"{max(a - 0.5, 0):.3f}", "-to", f"{b + 0.5:.3f}", "-i", str(media), "-vn",
                   "-ac", "1", "-ar", "24000", str(clip))
            segs = transcribe(clip, known)
            recovered_all[k] = [{"speaker": s["speaker"], "start": max(a - 0.5, 0) + s["start"],
                                 "end": max(a - 0.5, 0) + s["end"], "text": s["text"].strip()} for s in segs]
            clip.unlink(missing_ok=True)
            gap_cache.write_text(json.dumps(recovered_all, ensure_ascii=False, indent=1), encoding="utf-8")

        neighbours = " ".join(norm(s["text"]) for s in all_segments if a - 15 <= s["end"] and s["start"] <= b + 15)
        kept = []
        for s in recovered_all[k]:
            t = norm(s["text"])
            if not t or t in FILLERS or t in neighbours:
                continue      # empty, filler, or already in the transcript (padding overlap)
            kept.append({"speaker": s["speaker"] if s["speaker"] in known else "Unidentified speaker",
                         "start": s["start"], "end": s["end"], "text": s["text"], "recovered": True})
        if kept:
            all_segments.extend(kept)
        else:
            noise_only.append((a, b))
    all_segments.sort(key=lambda s: s["start"])
    recovered_count = sum(s["recovered"] for s in all_segments)

    # Write transcript: merge consecutive turns of the same speaker
    lines, current = [], None
    for s in all_segments:
        if s["recovered"]:
            lines += ["", f"[{fmt(s['start'])}] {s['speaker']}:  (recovered from faint audio)", s["text"]]
            current = None
        elif s["speaker"] != current:
            lines += ["", f"[{fmt(s['start'])}] {s['speaker']}:", s["text"]]
            current = s["speaker"]
        else:
            lines[-1] += " " + s["text"]
    (out_dir / "transcript.txt").write_text("\n".join(lines).strip() + "\n", encoding="utf-8")

    report = [
        f"Recording            : {media.name}",
        f"Duration             : {fmt(duration)}",
        f"Chunks               : {len(cuts) - 1} (cut at pauses, no overlap)",
        f"Speech covered       : {fmt(sum(s['end'] - s['start'] for s in all_segments))}",
        f"Words                : {sum(len(s['text'].split()) for s in all_segments)}",
        f"Speakers             : {', '.join(sorted({s['speaker'] for s in all_segments}))}",
        f"Gaps re-checked      : {len(gaps)}",
        f"Lines recovered      : {recovered_count} (marked 'recovered from faint audio')",
        f"Noise / no speech    : {len(noise_only)} stretch(es)",
    ] + [f"  {fmt(a)} - {fmt(b)}" for a, b in noise_only]
    (out_dir / "coverage_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    for line in report:
        log("      " + line)


# ============================================================
# STAGE 2 - ACTION ITEMS (+ CROSS-CHECK OF NOTES)
# ============================================================
SCHEMA_HINT = """Return JSON only, in exactly this shape:
{
  "title": "short meeting title",
  "priorities": ["only if the meeting explicitly named top priorities, else empty"],
  "sections": [
    {"heading": "TOPIC IN CAPITALS",
     "items": [
       {"text": "one clear action, imperative, max ~30 words",
        "details": ["optional sub-points with specifics: names, numbers, reasons"],
        "evidence": "[hh:mm:ss] short quote from the transcript, or 'not in recording'",
        "source": "notes_confirmed | notes_corrected | added_from_transcript | notes_not_in_recording"}
     ]}
  ],
  "corrections": [{"original": "user's note text", "corrected": "what it should say", "evidence": "[hh:mm:ss] quote"}],
  "removed": [{"original": "user's note text", "reason": "why it was dropped"}],
  "to_confirm": [{"item": "point", "reason": "why a human should check it"}],
  "notes_map": [{"note": "each line of the user's notes, verbatim", "item_number": 1}]
}
"notes_map" is only needed when the user gave notes: one entry per note line, with the number
(counting items across all sections, starting at 1) of the action item that covers it, or null if
the note was removed."""

RULES = """You turn a meeting transcript into a validated action-item list that will be pasted into
the Teamwork project tool and reviewed by a manager.

Rules:
- The transcript is machine-generated: names, acronyms and terms may be misheard. Speakers are only
  labels (Speaker 1, 2...). Use context to interpret, but never invent facts, names, numbers or owners.
- Only actionable items, decisions and instructions. Skip small talk, side conversations and status
  updates that need no action. Items that the meeting explicitly cancelled or "took off" are not actions
  (mention them under "removed" if they appeared in the notes).
- Group items into sections by topic. Keep each item short and specific enough to be a task title;
  put specifics (names, figures, reasons, examples) in "details".
- Quote figures exactly and completely, with their currency (e.g. "OMR 3.42 million", not "3.4").
- Cover the whole recording, including instructions given before or after the main agenda.
- Add to "to_confirm" anything uncertain: unclear names/spellings, ambiguous who-said-what, or a point
  whose meaning depends on context the recording doesn't show.
"""

NOTES_RULES = """The user also took their own notes during the meeting (below). Cross-check every note
against the transcript:
- Correct notes where the transcript shows different words, names, numbers or meaning
  (source = notes_corrected, and add an entry to "corrections" with the evidence).
- Keep notes that match (source = notes_confirmed); improve wording only for clarity.
- Notes not found in the recording: keep them (they may have been said before/after recording or
  off-mic) with source = notes_not_in_recording, written as the action the user noted (not as a
  "confirm whether..." question), and add them to "to_confirm".
- Never drop a note silently: a note that is corrected, or not found in the recording, must still
  appear as an action item.
- Merge duplicate notes into one item. Drop a note only if it is a duplicate or the meeting explicitly
  cancelled it - list it in "removed" with the reason.
- Add important actions from the transcript that the notes missed (source = added_from_transcript).
- Keep the user's own wording and section names where they are correct.
"""


def analyse(out_dir, notes_text, title, date):
    log("[4/4] Identifying action items" + (" and cross-checking your notes..." if notes_text else "..."))
    transcript = (out_dir / "transcript.txt").read_text(encoding="utf-8")
    system = RULES + (NOTES_RULES if notes_text else "") + "\n" + SCHEMA_HINT
    user = f"TRANSCRIPT:\n{transcript}"
    if notes_text:
        user += f"\n\nUSER'S NOTES:\n{notes_text}"
    resp = client.chat.completions.create(
        model=ANALYSIS_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content)

    # Safeguard: every note line must map to an action item or an explicit removal.
    # Anything unaccounted for is added back, so no note is ever lost.
    if notes_text:
        n_items = sum(len(s["items"]) for s in data.get("sections", []))
        mapped = {norm(m.get("note", "")): m.get("item_number") for m in data.get("notes_map", [])}
        removed = {norm(r.get("original", "")) for r in data.get("removed", [])}
        missing = []
        for raw in notes_text.splitlines():
            line = raw.strip(" -*#\t")
            if len(line.split()) < 3 or re.search(r"-{3,}|={3,}|#\s*#", raw):
                continue        # headings / separators
            k = norm(line)
            num = mapped.get(k)
            if k in removed or (isinstance(num, int) and 1 <= num <= n_items):
                continue
            if k in mapped and num is None and any(k in r or r in k for r in removed if r):
                continue
            missing.append(line)
        if missing:
            data.setdefault("sections", []).append({
                "heading": "FROM YOUR NOTES - NOT MATCHED TO THE RECORDING",
                "items": [{"text": m, "details": [], "evidence": "not in recording",
                           "source": "notes_not_in_recording"} for m in missing]})
            data.setdefault("to_confirm", []).extend(
                {"item": m, "reason": "Your note was not matched to any action point - kept as written"}
                for m in missing)

    (out_dir / "work" / "analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------- action_items.txt (clean, for Teamwork) ----------
    heading = title or data.get("title") or out_dir.name
    out = [heading.upper() + (f" ({date})" if date else "")]
    out.append("(Cross-checked against the meeting recording transcript)" if notes_text
               else "(Generated from the meeting recording transcript)")
    if data.get("priorities"):
        out += ["", "=" * 50, "TOP PRIORITIES (as stated in the meeting)", "=" * 50]
        out += [f"{i}. {p}" for i, p in enumerate(data["priorities"], 1)]
    out += ["", "=" * 50, "ACTION POINTS", "=" * 50]
    n = 0
    for sec in data.get("sections", []):
        out += ["", sec["heading"].upper(), ""]
        for item in sec["items"]:
            n += 1
            out.append(f"{n}. {item['text']}")
            out += [f"   - {d}" for d in item.get("details", [])]
            out.append("")
    (out_dir / "action_items.txt").write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    # ---------- review.txt (for the person validating) ----------
    labels = {"notes_confirmed": "CONFIRMED", "notes_corrected": "CORRECTED",
              "added_from_transcript": "ADDED FROM RECORDING", "notes_not_in_recording": "NOT IN RECORDING"}
    rv = [f"REVIEW - {heading}", ""]
    if data.get("corrections"):
        rv += ["CORRECTIONS TO NOTES", "-" * 50]
        for c in data["corrections"]:
            rv += [f"Note     : {c['original']}", f"Now      : {c['corrected']}", f"Evidence : {c['evidence']}", ""]
    if data.get("removed"):
        rv += ["REMOVED", "-" * 50]
        rv += [f"- {r['original']}  ->  {r['reason']}" for r in data["removed"]] + [""]
    if data.get("to_confirm"):
        rv += ["PLEASE CONFIRM", "-" * 50]
        rv += [f"- {t['item']}  ->  {t['reason']}" for t in data["to_confirm"]] + [""]
    rv += ["EVIDENCE FOR EVERY ACTION POINT", "-" * 50]
    n = 0
    for sec in data.get("sections", []):
        for item in sec["items"]:
            n += 1
            tag = labels.get(item.get("source"), "") if notes_text else ""
            rv.append(f"{n}. {item['text']}" + (f"   [{tag}]" if tag else ""))
            rv.append(f"   {item.get('evidence', '')}")
    (out_dir / "review.txt").write_text("\n".join(rv).rstrip() + "\n", encoding="utf-8")

    log(f"      {n} action points, {len(data.get('corrections', []))} corrections, "
        f"{len(data.get('to_confirm', []))} to confirm")


# ============================================================
# RUN
# ============================================================
def process(media, notes=None, title=None, date=None):
    media = Path(media).resolve()
    if not media.exists():
        sys.exit(f"File not found: {media}")
    out_dir = MEETINGS_DIR / media.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    auto_notes = media.with_name(f"{media.stem}_notes.txt")
    if not notes and auto_notes.exists():
        notes = auto_notes
    log("=" * 70)
    log(f"MEETING: {media.name}")
    log("=" * 70)
    notes_text = Path(notes).read_text(encoding="utf-8-sig") if notes else None
    if notes:
        (out_dir / "notes_used.txt").write_text(notes_text, encoding="utf-8")
    else:
        (out_dir / "notes_used.txt").unlink(missing_ok=True)
    build_transcript(media, out_dir)
    analyse(out_dir, notes_text, title, date)
    log("")
    log(f"Done -> {out_dir}")
    for f in ("transcript.txt", "coverage_report.txt", "action_items.txt", "review.txt"):
        log(f"   {f}")


def run_inbox():
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    files = [f for f in sorted(INBOX_DIR.iterdir()) if f.suffix.lower() in MEDIA_EXTS]
    if not files:
        log(f"No recordings in {INBOX_DIR}")
        return
    for media in files:
        notes = media.with_name(f"{media.stem}_notes.txt")
        result = MEETINGS_DIR / media.stem / "action_items.txt"
        if result.exists() and not (notes.exists() and notes.stat().st_mtime > result.stat().st_mtime):
            log(f"Skipping (already done): {media.name}")
            continue
        process(media, notes if notes.exists() else None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Recording -> transcript -> action items -> cross-check notes")
    ap.add_argument("media", nargs="?", help="audio or video file (any format ffmpeg reads)")
    ap.add_argument("--notes", help="txt file with your own notes to cross-check")
    ap.add_argument("--title", help="meeting title for the action list")
    ap.add_argument("--date", help="meeting date, e.g. '22 Oct 2026'")
    ap.add_argument("--inbox", action="store_true", help=f"process new recordings in {INBOX_DIR}")
    args = ap.parse_args()
    if args.inbox or not args.media:
        run_inbox()
    else:
        process(args.media, args.notes, args.title, args.date)
