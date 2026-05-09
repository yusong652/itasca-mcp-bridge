"""
Probe 2: verify PFC `program log` flush behavior.

Decides whether the per-itasca.command interleaving strategy is viable.

Question we need to answer:
  After itasca.command(...) returns, is the PFC log file content for that
  command already on disk? Or is PFC buffering writes and only flushing on
  `program log off`?

If buffered → we cannot do incremental reads between commands → fall back to
per-call on/off pairs (1.38ms/pair overhead per command).

Run with `%run path/to/probe_log_flush.py` from PFC IPython console.
Outputs to `probe_log_flush.<version>.out` next to itself.
"""

from __future__ import print_function

import itasca
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "flush_probe.log")


def _detect_version_tag():
    """Best-effort PFC version tag for the output filename."""
    try:
        # Open a tiny log with header to extract version line
        tag_log = os.path.join(HERE, "_version_tag.log")
        itasca.command("program log-file '{}'".format(tag_log.replace("\\", "/")))
        itasca.command("program log on truncate")  # show-message ON to get version line
        itasca.command("program log off")
        with open(tag_log, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Version" in line:
                    # e.g. "* By pfc3d Version 9.00 Release 184"
                    parts = line.strip("* \r\n").split()
                    for i, w in enumerate(parts):
                        if w == "Version" and i + 1 < len(parts):
                            return "pfc{}".format(parts[i + 1].split(".")[0])
    except Exception:
        pass
    finally:
        try:
            os.remove(tag_log)
        except Exception:
            pass
    return "unknown"


def _read_size_and_tail(path, tail_chars=400):
    if not os.path.exists(path):
        return 0, "<<file does not exist>>"
    size = os.path.getsize(path)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    tail = content[-tail_chars:] if len(content) > tail_chars else content
    return size, tail


def case(report, title, body):
    report.append("=" * 72)
    report.append("CASE: {}".format(title))
    report.append("-" * 72)
    report.append(body.rstrip())
    report.append("")


def main():
    report = []
    version_tag = _detect_version_tag()
    out_path = os.path.join(HERE, "probe_log_flush.{}.out".format(version_tag))

    # Set up a fresh model with content that produces predictable log output
    itasca.command("model new")
    itasca.command("model domain extent -5 5")
    itasca.command("ball create id 1 radius 0.5 position 0 0 0")
    itasca.command("ball create id 2 radius 0.5 position 1 0 0")

    # ------------------------------------------------------------------
    # Case A — single log session, multiple commands, sample size after each
    # ------------------------------------------------------------------
    log_path_norm = LOG_PATH.replace("\\", "/")
    itasca.command("program log-file '{}'".format(log_path_norm))
    itasca.command("program log on truncate show-message off")

    samples = []
    cmds = ["ball list", "ball list", "model list information", "ball list"]
    for i, cmd in enumerate(cmds):
        t0 = time.time()
        itasca.command(cmd)
        t1 = time.time()
        # Sample IMMEDIATELY after itasca.command returns
        size_after, tail_after = _read_size_and_tail(LOG_PATH, tail_chars=200)
        # Sample again after a short sleep
        time.sleep(0.05)
        size_after_sleep, _ = _read_size_and_tail(LOG_PATH, tail_chars=200)
        samples.append({
            "step": i,
            "cmd": cmd,
            "duration_ms": (t1 - t0) * 1000,
            "size_immediate": size_after,
            "size_after_50ms_sleep": size_after_sleep,
            "tail_immediate": tail_after,
        })

    itasca.command("program log off")
    final_size, final_tail = _read_size_and_tail(LOG_PATH, tail_chars=400)

    body_lines = []
    body_lines.append("Single session, sampled size after each itasca.command:")
    body_lines.append("")
    prev_size = 0
    for s in samples:
        delta = s["size_immediate"] - prev_size
        delta_after_sleep = s["size_after_50ms_sleep"] - s["size_immediate"]
        body_lines.append(
            "  step {step}  cmd={cmd!r:<30s}  cmd_dur={dur:6.1f}ms  "
            "size_now={now:>7d}  Δ={delta:>+6d}  "
            "size_after_50ms={late:>7d}  late_Δ={late_delta:>+6d}".format(
                step=s["step"], cmd=s["cmd"], dur=s["duration_ms"],
                now=s["size_immediate"], delta=delta,
                late=s["size_after_50ms_sleep"], late_delta=delta_after_sleep,
            )
        )
        prev_size = s["size_after_50ms_sleep"]
    body_lines.append("")
    body_lines.append("After `program log off`, final size = {}".format(final_size))
    body_lines.append("")
    body_lines.append("Final log tail (last 400 chars):")
    body_lines.append(final_tail)
    body_lines.append("")
    body_lines.append("INTERPRETATION:")
    body_lines.append(
        "  - If `Δ` per step is large (matches expected output) → flushed synchronously, "
        "incremental read works."
    )
    body_lines.append(
        "  - If `Δ` is 0 and `late_Δ` jumps after sleep → buffered, sleep helps but unreliable."
    )
    body_lines.append(
        "  - If `Δ` and `late_Δ` are both 0 until log off → only flushes on close, "
        "must use per-call on/off."
    )
    case(report, "A. Sync flush check (single session, sample after each command)", "\n".join(body_lines))

    # ------------------------------------------------------------------
    # Case B — does `program log off; on append` force a flush mid-session?
    # ------------------------------------------------------------------
    itasca.command("program log-file '{}'".format(log_path_norm))
    itasca.command("program log on truncate show-message off")

    itasca.command("ball list")
    size_before_toggle, _ = _read_size_and_tail(LOG_PATH, tail_chars=80)

    # Toggle off then on (append) — should flush whatever is buffered
    itasca.command("program log off")
    size_after_off, _ = _read_size_and_tail(LOG_PATH, tail_chars=80)

    itasca.command("program log on show-message off")  # default = append
    itasca.command("ball list")
    itasca.command("program log off")
    size_final, tail_final = _read_size_and_tail(LOG_PATH, tail_chars=200)

    body = (
        "After 1st `ball list` (still inside log session): size = {}\n"
        "After `program log off`:                          size = {}\n"
        "After 2nd ball list + log off (append mode):      size = {}\n"
        "\n"
        "If `size_after_off` jumped vs `size_before_toggle`, then `log off` flushed buffered data.\n"
        "If they're equal, content was already on disk before the toggle.\n"
        "\n"
        "Final tail (last 200 chars):\n{}"
    ).format(size_before_toggle, size_after_off, size_final, tail_final)
    case(report, "B. Does log off/on toggle force flush (and append work)?", body)

    # ------------------------------------------------------------------
    # Case C — per-call on/off baseline (overhead measurement)
    # ------------------------------------------------------------------
    N = 50
    itasca.command("program log-file '{}'".format(log_path_norm))
    t0 = time.time()
    for _ in range(N):
        itasca.command("program log on truncate show-message off")
        itasca.command("ball list")
        itasca.command("program log off")
    per_call_dt = time.time() - t0

    body = (
        "N = {n}\n"
        "Per-call (log on truncate / cmd / log off) total: {dt:.3f}s ({per:.2f} ms/call)\n"
        "\n"
        "This is the fallback strategy if synchronous flush doesn't work.\n"
        "Compare to `bare itasca.command` baseline from probe 1 (~1ms/call on PFC 7,\n"
        "~9ms/call on PFC 9 for `ball list`)."
    ).format(n=N, dt=per_call_dt, per=per_call_dt * 1000 / N)
    case(report, "C. Per-call on/off overhead (fallback strategy)", body)

    # Cleanup
    if os.path.exists(LOG_PATH):
        try:
            os.remove(LOG_PATH)
        except Exception:
            pass

    text = "\n".join(report) + "\n"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    print("Probe complete. Report written to: {}".format(out_path))
    print()
    print(text)


if __name__ == "__main__":
    main()
