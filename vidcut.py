#!/usr/bin/env python3
"""
vidcut.py — Remove time ranges from a video file.

Usage:
  python vidcut.py params.json   # command-line mode
  python vidcut.py               # open GUI

params.json:
{
  "input":  "source.mp4",
  "output": "result.mp4",
  "cuts": [
    ["00:01:00:000", "00:02:30:500"],
    ["3750", "4200.75"]
  ]
}

Timestamp formats accepted:
  HH:MM:SS:mmm   hours, minutes, seconds, milliseconds
  SS or SS.sss   plain seconds (integer or decimal)
  HH:MM:SS       hours, minutes, seconds
  MM:SS          minutes, seconds
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def parse_timestamp(ts: str) -> float:
    """Return seconds as a float.  Accepts HH:MM:SS:mmm or plain seconds."""
    parts = str(ts).strip().split(":")
    n = len(parts)
    try:
        if n == 1:
            return float(parts[0])
        if n == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if n == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if n == 4:
            h, m, s, ms = parts
            return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
    except ValueError:
        pass
    raise ValueError(f"Cannot parse timestamp: {ts!r}")


# ---------------------------------------------------------------------------
# Interval arithmetic
# ---------------------------------------------------------------------------

def merge_intervals(ivs: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not ivs:
        return []
    ivs = sorted(ivs)
    merged: list[list[float]] = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def invert_intervals(
    cuts: list[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """Return the segments to KEEP (complement of cuts within [0, duration])."""
    merged = merge_intervals(cuts)
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in merged:
        s = max(s, 0.0)
        e = min(e, duration)
        if cursor < s - 1e-6:
            kept.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration - 1e-6:
        kept.append((cursor, duration))
    return kept


# ---------------------------------------------------------------------------
# Validation (shared between CLI and GUI)
# ---------------------------------------------------------------------------

def validate_and_parse_cuts(
    src: str,
    dst: str,
    raw_cuts: list,
) -> list[tuple[float, float]]:
    """Raise ValueError on any problem; return parsed cut list on success."""
    if not os.path.isfile(src):
        raise ValueError(f"Input file not found: {src}")
    if Path(src).resolve() == Path(dst).resolve():
        raise ValueError("Input and output must be different files.")
    if not Path(dst).parent.exists():
        raise ValueError(f"Output directory does not exist: {Path(dst).parent}")
    if not isinstance(raw_cuts, list):
        raise ValueError("'cuts' must be a list.")

    cuts: list[tuple[float, float]] = []
    for i, cut in enumerate(raw_cuts):
        if not isinstance(cut, (list, tuple)) or len(cut) != 2:
            raise ValueError(f"Cut #{i}: expected a [start, end] pair, got {cut!r}")
        s_str, e_str = str(cut[0]).strip(), str(cut[1]).strip()
        if not s_str or not e_str:
            raise ValueError(f"Cut #{i}: timestamps must not be empty.")
        t0 = parse_timestamp(s_str)
        t1 = parse_timestamp(e_str)
        if t0 >= t1:
            raise ValueError(f"Cut #{i}: start ({cut[0]!r}) must be before end ({cut[1]!r}).")
        cuts.append((t0, t1))
    return cuts


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# ---------------------------------------------------------------------------

def _check_tool(name: str) -> None:
    if shutil.which(name) is None:
        print(f"Error: '{name}' not found on PATH.  Please install ffmpeg.", file=sys.stderr)
        sys.exit(1)


def _require_tools() -> None:
    for t in ("ffmpeg", "ffprobe"):
        if shutil.which(t) is None:
            raise RuntimeError(f"'{t}' not found on PATH.  Please install ffmpeg.")


def probe_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def extract_segment(src: str, start: float, end: float, dst: str) -> None:
    """Copy one time range from src to dst without re-encoding."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-ss", f"{start:.6f}",
            "-to", f"{end:.6f}",
            "-i", src,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            dst,
        ],
        check=True,
    )


# ---------------------------------------------------------------------------
# Core operation  (progress_cb receives (fraction 0..1, message str))
# ---------------------------------------------------------------------------

ProgressCb = Callable[[float, str], None]


def do_cut(
    src: str,
    dst: str,
    cuts: list[tuple[float, float]],
    progress_cb: ProgressCb | None = None,
) -> None:
    """Execute the cut.  Raises RuntimeError on failure."""

    def report(f: float, msg: str) -> None:
        if progress_cb:
            progress_cb(f, msg)

    _require_tools()
    report(0.0, "Probing input …")

    try:
        duration = probe_duration(src)
    except subprocess.CalledProcessError:
        raise RuntimeError("ffprobe failed — is the input a valid video file?")

    segments = invert_intervals(cuts, duration)
    if not segments:
        raise RuntimeError("No content remains after applying all cuts.")

    output_duration = sum(e - s for s, e in segments)
    n = len(segments)

    with tempfile.TemporaryDirectory(prefix="vidcut_") as tmp:
        seg_paths: list[str] = []

        # Phase 1 — extract kept segments (covers 0 % → 60 %)
        for i, (s, e) in enumerate(segments):
            report(
                i / n * 0.6,
                f"Extracting segment {i + 1}/{n}  [{s:.3f}s → {e:.3f}s]",
            )
            seg_file = os.path.join(tmp, f"seg_{i:04d}.mp4")
            try:
                extract_segment(src, s, e, seg_file)
            except subprocess.CalledProcessError:
                raise RuntimeError(f"ffmpeg failed while extracting segment {i + 1}.")
            seg_paths.append(seg_file)

        report(0.6, f"All {n} segment(s) extracted.")

        # Phase 2 — concatenate (covers 60 % → 100 %)
        if len(seg_paths) == 1:
            shutil.copy2(seg_paths[0], dst)
            report(1.0, "Done.")
            return

        concat_list = os.path.join(tmp, "concat.txt")
        with open(concat_list, "w") as fh:
            for sp in seg_paths:
                fh.write(f"file '{sp}'\n")

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-c", "copy",
            "-progress", "pipe:1",
            dst,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_ms="):
                try:
                    t = int(line.split("=")[1]) / 1_000_000
                    frac = 0.6 + min(t / output_duration, 1.0) * 0.4
                    report(frac, f"Concatenating … {t:.1f}s / {output_duration:.1f}s")
                except ValueError:
                    pass
            elif line == "progress=end":
                report(1.0, f"Concatenating … {output_duration:.1f}s / {output_duration:.1f}s")
        proc.wait()
        if proc.returncode != 0:
            err = proc.stderr.read()
            raise RuntimeError(f"ffmpeg concat failed:\n{err}")


# ---------------------------------------------------------------------------
# CLI progress bar
# ---------------------------------------------------------------------------

def _bar(frac: float, label: str = "", width: int = 45) -> str:
    frac = max(0.0, min(frac, 1.0))
    filled = round(frac * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"\r[{bar}] {frac * 100:5.1f}%  {label}"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def run_cli(param_path: str) -> None:
    _check_tool("ffmpeg")
    _check_tool("ffprobe")

    if not os.path.isfile(param_path):
        print(f"Parameter file not found: {param_path}", file=sys.stderr)
        sys.exit(1)

    with open(param_path) as f:
        try:
            p = json.load(f)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON: {exc}", file=sys.stderr)
            sys.exit(1)

    for key in ("input", "output", "cuts"):
        if key not in p:
            print(f"Missing required key: {key!r}", file=sys.stderr)
            sys.exit(1)

    src, dst = str(p["input"]), str(p["output"])

    try:
        cuts = validate_and_parse_cuts(src, dst, p["cuts"])
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    def cli_cb(frac: float, msg: str) -> None:
        print(_bar(frac, msg), end="", flush=True)

    try:
        do_cut(src, dst, cuts, progress_cb=cli_cb)
    except RuntimeError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)

    out_size = Path(dst).stat().st_size / (1024 * 1024)
    print(f"\n\nDone.  Saved {out_size:.1f} MB → {dst}")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class App:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            root.title("vidcut")
            root.resizable(True, True)
            root.minsize(580, 440)
            # (StringVar-start, StringVar-end, row-Frame)
            self._cut_rows: list[tuple[tk.StringVar, tk.StringVar, tk.Frame]] = []
            self._q: queue.Queue[tuple[str, float, str]] = queue.Queue()
            self._build_ui()

        # ── Layout ────────────────────────────────────────────────────────

        def _build_ui(self) -> None:
            P = {"padx": 10, "pady": 5}

            # Files
            files = ttk.LabelFrame(self.root, text="Files", padding=8)
            files.grid(row=0, column=0, sticky="ew", **P)
            files.columnconfigure(1, weight=1)

            ttk.Label(files, text="Input:").grid(row=0, column=0, sticky="w")
            self._in_var = tk.StringVar()
            ttk.Entry(files, textvariable=self._in_var).grid(
                row=0, column=1, sticky="ew", padx=(6, 4)
            )
            ttk.Button(files, text="Browse…", command=self._browse_input).grid(row=0, column=2)

            ttk.Label(files, text="Output:").grid(row=1, column=0, sticky="w", pady=(6, 0))
            self._out_var = tk.StringVar()
            ttk.Entry(files, textvariable=self._out_var).grid(
                row=1, column=1, sticky="ew", padx=(6, 4), pady=(6, 0)
            )
            ttk.Button(files, text="Browse…", command=self._browse_output).grid(
                row=1, column=2, pady=(6, 0)
            )

            # Cuts
            cuts_frame = ttk.LabelFrame(
                self.root, text="Cuts  (time ranges to remove)", padding=8
            )
            cuts_frame.grid(row=1, column=0, sticky="nsew", **P)
            cuts_frame.columnconfigure(0, weight=1)
            cuts_frame.rowconfigure(1, weight=1)

            # Column headers + Add button
            hdr = ttk.Frame(cuts_frame)
            hdr.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
            ttk.Label(hdr, text="#", width=3, anchor="center").pack(side="left")
            ttk.Label(hdr, text="Start timestamp", width=20).pack(side="left", padx=(4, 0))
            ttk.Label(hdr, text="End timestamp", width=20).pack(side="left", padx=(4, 0))
            ttk.Button(hdr, text="＋ Add Cut", command=self._add_cut).pack(side="right")

            # Scrollable cut list
            self._canvas = tk.Canvas(cuts_frame, height=150, highlightthickness=0)
            sb = ttk.Scrollbar(cuts_frame, orient="vertical", command=self._canvas.yview)
            self._canvas.configure(yscrollcommand=sb.set)
            self._canvas.grid(row=1, column=0, sticky="nsew")
            sb.grid(row=1, column=1, sticky="ns")
            self._inner = ttk.Frame(self._canvas)
            self._win_id = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
            self._inner.bind(
                "<Configure>",
                lambda _: self._canvas.configure(scrollregion=self._canvas.bbox("all")),
            )
            self._canvas.bind(
                "<Configure>",
                lambda e: self._canvas.itemconfig(self._win_id, width=e.width),
            )

            # Action bar
            actions = ttk.Frame(self.root, padding=(10, 2))
            actions.grid(row=2, column=0, sticky="ew")
            ttk.Button(actions, text="Load JSON…", command=self._load_json).pack(side="left")
            ttk.Button(actions, text="Save JSON…", command=self._save_json).pack(
                side="left", padx=6
            )
            self._run_btn = ttk.Button(actions, text="▶  Run", command=self._run)
            self._run_btn.pack(side="right")

            # Progress
            prog = ttk.Frame(self.root, padding=(10, 0, 10, 10))
            prog.grid(row=3, column=0, sticky="ew")
            prog.columnconfigure(0, weight=1)
            self._frac_var = tk.DoubleVar(value=0.0)
            ttk.Progressbar(
                prog, variable=self._frac_var, maximum=1.0, mode="determinate"
            ).grid(row=0, column=0, sticky="ew")
            self._pct_lbl = ttk.Label(prog, text="  0 %", width=6, anchor="e")
            self._pct_lbl.grid(row=0, column=1)
            self._status_var = tk.StringVar(value="Ready.")
            ttk.Label(prog, textvariable=self._status_var, anchor="w").grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0)
            )

            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(1, weight=1)

        # ── Cut row management ────────────────────────────────────────────

        def _add_cut(self, start: str = "", end: str = "") -> None:
            idx = len(self._cut_rows)
            row = ttk.Frame(self._inner)
            row.pack(fill="x", pady=2)

            sv, ev = tk.StringVar(value=start), tk.StringVar(value=end)
            self._num_label = ttk.Label(row, text=f"{idx + 1}.", width=3, anchor="e")
            self._num_label.pack(side="left")
            ttk.Entry(row, textvariable=sv, width=20).pack(side="left", padx=(4, 0))
            ttk.Entry(row, textvariable=ev, width=20).pack(side="left", padx=(4, 0))
            ttk.Button(
                row, text="✕", width=3, command=lambda r=row: self._remove_cut(r)
            ).pack(side="right", padx=(0, 2))

            self._cut_rows.append((sv, ev, row))
            self._renumber()

        def _remove_cut(self, target: tk.Frame) -> None:
            self._cut_rows = [(s, e, r) for s, e, r in self._cut_rows if r is not target]
            target.destroy()
            self._renumber()

        def _renumber(self) -> None:
            for i, (_, _, row) in enumerate(self._cut_rows):
                for w in row.winfo_children():
                    if isinstance(w, ttk.Label):
                        w.configure(text=f"{i + 1}.")
                        break

        # ── File dialogs ──────────────────────────────────────────────────

        def _browse_input(self) -> None:
            p = filedialog.askopenfilename(
                title="Select input video",
                filetypes=[
                    ("Video files", "*.mp4 *.mov *.mkv *.avi *.m4v"),
                    ("All files", "*.*"),
                ],
            )
            if p:
                self._in_var.set(p)
                if not self._out_var.get():
                    pp = Path(p)
                    self._out_var.set(str(pp.parent / f"{pp.stem}_cut{pp.suffix}"))

        def _browse_output(self) -> None:
            p = filedialog.asksaveasfilename(
                title="Save output video as",
                defaultextension=".mp4",
                filetypes=[("MP4", "*.mp4"), ("All files", "*.*")],
            )
            if p:
                self._out_var.set(p)

        # ── JSON I/O ──────────────────────────────────────────────────────

        def _load_json(self) -> None:
            p = filedialog.askopenfilename(
                title="Load parameter file",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not p:
                return
            try:
                with open(p) as f:
                    data = json.load(f)
            except Exception as exc:
                messagebox.showerror("Load error", str(exc))
                return

            self._in_var.set(data.get("input", ""))
            self._out_var.set(data.get("output", ""))
            for _, _, row in self._cut_rows:
                row.destroy()
            self._cut_rows.clear()
            for cut in data.get("cuts", []):
                if isinstance(cut, (list, tuple)) and len(cut) == 2:
                    self._add_cut(str(cut[0]), str(cut[1]))

        def _save_json(self) -> None:
            p = filedialog.asksaveasfilename(
                title="Save parameter file",
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            )
            if not p:
                return
            data = {
                "input": self._in_var.get().strip(),
                "output": self._out_var.get().strip(),
                "cuts": [
                    [sv.get().strip(), ev.get().strip()]
                    for sv, ev, _ in self._cut_rows
                ],
            }
            try:
                with open(p, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception as exc:
                messagebox.showerror("Save error", str(exc))

        # ── Run ───────────────────────────────────────────────────────────

        def _run(self) -> None:
            src = self._in_var.get().strip()
            dst = self._out_var.get().strip()
            raw = [[sv.get().strip(), ev.get().strip()] for sv, ev, _ in self._cut_rows]

            if not src or not dst:
                messagebox.showerror("Missing input", "Please set both input and output files.")
                return
            if not raw:
                messagebox.showerror("No cuts", "Add at least one cut before running.")
                return

            try:
                cuts = validate_and_parse_cuts(src, dst, raw)
            except ValueError as exc:
                messagebox.showerror("Validation error", str(exc))
                return

            self._run_btn.state(["disabled"])
            self._frac_var.set(0.0)
            self._pct_lbl.configure(text="  0 %")
            self._status_var.set("Starting…")

            def worker() -> None:
                try:
                    do_cut(src, dst, cuts, progress_cb=lambda f, m: self._q.put(("prog", f, m)))
                    size_mb = Path(dst).stat().st_size / (1024 * 1024)
                    self._q.put(("done", 1.0, f"Saved {size_mb:.1f} MB → {dst}"))
                except Exception as exc:
                    self._q.put(("error", 0.0, str(exc)))

            threading.Thread(target=worker, daemon=True).start()
            self.root.after(50, self._poll)

        def _poll(self) -> None:
            try:
                while True:
                    kind, frac, msg = self._q.get_nowait()
                    self._frac_var.set(frac)
                    self._pct_lbl.configure(text=f"{frac * 100:4.0f} %")
                    self._status_var.set(msg)
                    if kind == "done":
                        self._run_btn.state(["!disabled"])
                        messagebox.showinfo("Done", msg)
                        return
                    if kind == "error":
                        self._run_btn.state(["!disabled"])
                        messagebox.showerror("Error", msg)
                        return
            except queue.Empty:
                pass
            self.root.after(50, self._poll)

    root = tk.Tk()
    App(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) == 1:
        run_gui()
    elif len(sys.argv) == 2:
        run_cli(sys.argv[1])
    else:
        print(f"Usage: {Path(sys.argv[0]).name} [params.json]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
