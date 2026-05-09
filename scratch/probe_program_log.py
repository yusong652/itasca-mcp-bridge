"""
Probe: verify `program log` capture behavior inside PFC GUI.

Run with `%run path/to/probe_program_log.py` from PFC IPython console
(PFC 7 or Itasca 9). The script writes results to `probe_program_log.out`
next to itself for inspection.

What this verifies (decides whether the capture-command-output feature
can be wired into pfc-mcp-bridge/.../execution/script.py):

  1. Does `program log on/off` actually capture `itasca.command()` output?
  2. Does `truncate` reset the file each snippet?
  3. Does `show-message off` suppress the date/time/log header?
  4. Does the log capture command errors too (so we still get partial
     output when the user's command raises)?
  5. What is the per-call overhead of wrapping with log on/off?
"""

from __future__ import print_function

import itasca
import os
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "probe.log")
OUT_PATH = os.path.join(HERE, "probe_program_log.out")


def _read_log():
    if not os.path.exists(LOG_PATH):
        return "<<no log file produced>>"
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _start_log(truncate=True, show_message=False):
    itasca.command("program log-file '{}'".format(LOG_PATH.replace("\\", "/")))
    pieces = ["program log on"]
    if truncate:
        pieces.append("truncate")
    if not show_message:
        pieces.append("show-message off")
    itasca.command(" ".join(pieces))


def _stop_log():
    itasca.command("program log off")


def case(report, title, body):
    report.append("=" * 72)
    report.append("CASE: {}".format(title))
    report.append("-" * 72)
    report.append(body.rstrip())
    report.append("")


def main():
    report = []

    # --- Fresh model so cases are reproducible ---
    itasca.command("model new")
    itasca.command("model domain extent -5 5")
    itasca.command("ball create id 1 radius 0.5 position 0 0 0")
    itasca.command("ball create id 2 radius 0.5 position 1 0 0")

    # ------------------------------------------------------------------
    # Case 1 — basic capture of `ball list`
    # ------------------------------------------------------------------
    _start_log()
    itasca.command("ball list")
    _stop_log()
    case(report, "1. ball list (2 balls present)", _read_log())

    # ------------------------------------------------------------------
    # Case 2 — multi-line summary output
    # ------------------------------------------------------------------
    _start_log()
    itasca.command("model list information")
    _stop_log()
    case(report, "2. model list information", _read_log())

    # ------------------------------------------------------------------
    # Case 3 — show-message ON for comparison
    # ------------------------------------------------------------------
    _start_log(show_message=True)
    itasca.command("ball list")
    _stop_log()
    case(report, "3. ball list with show-message ON (should add header)", _read_log())

    # ------------------------------------------------------------------
    # Case 4 — truncate: run twice, expect only second snippet's output
    # ------------------------------------------------------------------
    _start_log()
    itasca.command("ball list")
    _stop_log()
    _start_log()  # truncate=True (default)
    itasca.command("model list information")
    _stop_log()
    case(report, "4. truncate=True between calls (should see ONLY model list info)", _read_log())

    # ------------------------------------------------------------------
    # Case 5 — error case: bad command, does log still flush?
    # ------------------------------------------------------------------
    _start_log()
    err_repr = "<no exception>"
    try:
        itasca.command("ball list")
        itasca.command("ball this-keyword-does-not-exist")
    except BaseException as e:
        err_repr = "{}: {}".format(type(e).__name__, e)
    finally:
        _stop_log()
    body = "Python-side exception: {}\n---LOG---\n{}".format(err_repr, _read_log())
    case(report, "5. error after a successful command (log content + exception)", body)

    # ------------------------------------------------------------------
    # Case 6 — overhead: 100 trivial command calls, with and without wrapping
    # ------------------------------------------------------------------
    N = 100

    t0 = time.time()
    for _ in range(N):
        itasca.command("model list information")
    bare_dt = time.time() - t0

    t0 = time.time()
    _start_log()
    for _ in range(N):
        itasca.command("model list information")
    _stop_log()
    wrapped_dt = time.time() - t0

    body = (
        "N = {}\n"
        "  bare itasca.command loop:    {:.3f}s ({:.2f} ms/call)\n"
        "  wrapped (log on for whole loop): {:.3f}s ({:.2f} ms/call)\n"
        "  overhead per call:           {:.2f} ms\n"
        "(Note: this is amortized — real cost is the 2 extra commands per snippet,\n"
        " not per inner itasca.command. Per-snippet overhead is what matters.)"
    ).format(N, bare_dt, bare_dt * 1000 / N, wrapped_dt, wrapped_dt * 1000 / N,
             (wrapped_dt - bare_dt) * 1000 / N)
    case(report, "6. overhead measurement", body)

    # ------------------------------------------------------------------
    # Case 7 — per-snippet wrap overhead (log on/off pair, no inner work)
    # ------------------------------------------------------------------
    M = 50
    t0 = time.time()
    for _ in range(M):
        _start_log()
        _stop_log()
    pair_dt = time.time() - t0
    body = (
        "M = {}\n"
        "  bare on/off pair: {:.3f}s ({:.2f} ms/pair)\n"
        "(This is the overhead added to *every* execute_code/execute_task call.)"
    ).format(M, pair_dt, pair_dt * 1000 / M)
    case(report, "7. log on/off pair overhead", body)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    if os.path.exists(LOG_PATH):
        try:
            os.remove(LOG_PATH)
        except Exception:
            pass

    text = "\n".join(report) + "\n"
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(text)

    print("Probe complete. Report written to: {}".format(OUT_PATH))
    print()
    print(text)


if __name__ == "__main__":
    main()
