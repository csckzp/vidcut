# vidcut

A fast Python tool for removing time ranges from video files. Runs as a command-line tool or a desktop GUI — no re-encoding, no quality loss.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## How it works

You supply a list of **cuts** — time ranges to *remove*. vidcut keeps everything outside those ranges and stitches the kept segments back together using ffmpeg stream copy, meaning the video is never decoded or re-encoded. A typical edit of an hour-long video takes a few seconds.

---

## Requirements

- Python 3.10 or later
- [ffmpeg](https://ffmpeg.org/download.html) (both `ffmpeg` and `ffprobe` must be on your `PATH`)

No Python packages beyond the standard library are required.

---

## Installation

```bash
# Clone or download vidcut.py, then verify ffmpeg is available:
ffmpeg -version
ffprobe -version
```

That's it — there is nothing to install for Python itself.

---

## Usage

### GUI mode

Launch with no arguments:

```bash
python vidcut.py
```

- Pick an input file with **Browse** — the output path is pre-filled as `<name>_cut.<ext>` in the same folder.
- Add cuts with **＋ Add Cut**, fill in start and end timestamps, remove any with **✕**.
- **Load JSON / Save JSON** round-trip the parameter file format so you can save a set of cuts and reuse them later from the CLI.
- Click **▶ Run**. A progress bar tracks extraction (0–60 %) and concatenation (60–100 %).

### CLI mode

```bash
python vidcut.py params.json
```

---

## Parameter file

The parameter file is JSON with three keys:

| Key | Type | Description |
|-----|------|-------------|
| `input` | string | Path to the source video file |
| `output` | string | Path for the edited output file (must differ from input) |
| `cuts` | array | List of `[start, end]` pairs — the ranges to **remove** |

```json
{
  "input":  "lecture.mp4",
  "output": "lecture_edited.mp4",
  "cuts": [
    ["00:00:00:000", "00:00:45:000"],
    ["00:32:10:000", "00:33:05:500"],
    ["5520",         "5580"]
  ]
}
```

---

## Timestamp format

Each timestamp is a string in one of these formats:

| Format | Example | Meaning |
|--------|---------|---------|
| `HH:MM:SS:mmm` | `01:02:03:500` | 1 h 2 min 3.5 s |
| `HH:MM:SS` | `01:02:03` | 1 h 2 min 3 s |
| `MM:SS` | `02:03` | 2 min 3 s |
| `SS` or `SS.sss` | `90` or `90.5` | Plain seconds |

All timestamps are relative to the **start of the input file**.

---

## Notes

**Cut accuracy.** Because stream copy is used, cut boundaries snap to the nearest keyframe in the source video. In practice this is usually within 0.5–2 seconds of the requested time. If you need frame-perfect cuts the video would need to be re-encoded (not supported by this tool).

**Overlapping cuts** are merged automatically, so you don't need to worry about ordering or overlap in the parameter file.

**Audio** is preserved without re-encoding alongside the video.

**Output directory** must already exist; vidcut will not create it.

---

## License

MIT
