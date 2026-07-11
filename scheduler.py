#!/usr/bin/env python3
"""
Stake Dice Bot — Auto Scheduler
Siklus: Jalan 30 menit → Jeda 10 menit → Ulangi tak terbatas

Jalankan: python scheduler.py
Hentikan: Ctrl+C
"""

import subprocess, sys, os, time, threading, signal
from datetime import datetime, timedelta

# ── Konfigurasi siklus ────────────────────────────────────
RUN_MINUTES   = 30    # durasi bot aktif per siklus
PAUSE_MINUTES = 10    # durasi jeda antar siklus
BOT_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dice_bot.py")

# ── Helpers display ───────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")

def fmt_dur(seconds):
    """Format detik → MM:SS"""
    m, s = divmod(int(max(0, seconds)), 60)
    return f"{m:02d}:{s:02d}"

def fmt_dur_long(seconds):
    """Format detik → Xj Ym atau Xm Ys"""
    seconds = int(max(0, seconds))
    h, rem  = divmod(seconds, 3600)
    m, s    = divmod(rem, 60)
    if h:
        return f"{h}j {m}m"
    return f"{m}m {s}s"

def print_banner():
    print("\n" + "╔" + "═"*60 + "╗")
    print("║" + "  STAKE DICE BOT — AUTO SCHEDULER".center(60) + "║")
    print("║" + f"  ▶ Jalan {RUN_MINUTES} mnt  →  ⏸ Jeda {PAUSE_MINUTES} mnt  →  ♻  Ulangi".center(60) + "║")
    print("║" + "  Ctrl+C untuk berhenti kapanpun".center(60) + "║")
    print("╚" + "═"*60 + "╝\n")

def print_run_header(cycle, start_at, stop_at):
    print("┌" + "─"*60 + "┐")
    print("│" + f"  ▶  SIKLUS #{cycle}  —  BOT JALAN".ljust(60) + "│")
    print("│" + f"  Mulai : {start_at}   |   Stop  : {stop_at}   |   Durasi: {RUN_MINUTES} mnt".ljust(60) + "│")
    print("└" + "─"*60 + "┘")

def print_pause_header(cycle, start_at, resume_at):
    print("\n" + "┌" + "─"*60 + "┐")
    print("│" + f"  ⏸  SIKLUS #{cycle}  —  BOT DIJEDA".ljust(60) + "│")
    print("│" + f"  Mulai jeda : {start_at}   |   Lanjut : {resume_at}".ljust(60) + "│")
    print("└" + "─"*60 + "┘")

def print_status_tick(label, remaining, total, cycle, extra=""):
    done  = int((1 - remaining / total) * 24)
    bar   = "█" * done + "░" * (24 - done)
    pct   = int((1 - remaining / total) * 100)
    print(f"\r  {label} [{bar}] {fmt_dur(remaining)} sisa  ({pct}%)  {extra}  ", end="", flush=True)

def print_separator(title=""):
    if title:
        pad = (60 - len(title) - 2) // 2
        print(f"\n  {'─'*pad} {title} {'─'*pad}")
    else:
        print("  " + "─"*60)

# ── Output forwarder ──────────────────────────────────────
def forward_output(proc, stop_event, line_count):
    """Thread: baca stdout bot dan cetak ke terminal."""
    for line in proc.stdout:
        if stop_event.is_set():
            break
        print(line, end="", flush=True)
        line_count[0] += 1

# ── Fase JALAN ────────────────────────────────────────────
def run_phase(cycle, stats):
    duration = RUN_MINUTES * 60
    start    = time.monotonic()
    start_dt = datetime.now()
    stop_dt  = start_dt + timedelta(seconds=duration)

    print_run_header(cycle, start_dt.strftime("%H:%M:%S"), stop_dt.strftime("%H:%M:%S"))

    proc         = None
    stop_event   = threading.Event()
    line_count   = [0]
    restarts     = 0

    try:
        while True:
            elapsed   = time.monotonic() - start
            remaining = duration - elapsed
            if remaining <= 0:
                break

            # Mulai (atau restart) bot jika belum jalan
            if proc is None or proc.poll() is not None:
                if proc is not None:
                    # Bot mati sebelum waktunya — restart
                    restarts += 1
                    stop_event.set()
                    stop_event = threading.Event()
                    print_separator(f"Bot berhenti sendiri — restart #{restarts} | {now_str()}")

                proc = subprocess.Popen(
                    [sys.executable, BOT_SCRIPT],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=1,
                    text=True,
                )
                reader = threading.Thread(
                    target=forward_output,
                    args=(proc, stop_event, line_count),
                    daemon=True
                )
                reader.start()

            # Status ticker tiap 5 menit
            elapsed_int = int(elapsed)
            if elapsed_int > 0 and elapsed_int % 300 == 0 and elapsed_int != stats.get("last_tick_run"):
                stats["last_tick_run"] = elapsed_int
                print(f"\n  [⏱  {now_str()}  |  Sisa jalan: {fmt_dur(remaining)}"
                      f"  |  Siklus #{cycle}  |  Baris log: {line_count[0]}]\n", flush=True)

            time.sleep(0.5)

    finally:
        stop_event.set()
        if proc and proc.poll() is None:
            print(f"\n  [{now_str()}]  ⏹  Menghentikan bot (SIGTERM)...", flush=True)
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    elapsed_real = time.monotonic() - start
    stats["total_run"]    += elapsed_real
    stats["total_lines"]  += line_count[0]
    stats["restarts"]     += restarts

    print_separator(f"Siklus #{cycle} selesai — {now_str()}")
    print(f"  Durasi jalan : {fmt_dur_long(elapsed_real)}")
    print(f"  Baris log    : {line_count[0]}  |  Restart bot: {restarts}")
    return elapsed_real

# ── Fase JEDA ─────────────────────────────────────────────
def pause_phase(cycle, stats):
    duration  = PAUSE_MINUTES * 60
    start     = time.monotonic()
    start_dt  = datetime.now()
    resume_dt = start_dt + timedelta(seconds=duration)

    print_pause_header(cycle, start_dt.strftime("%H:%M:%S"), resume_dt.strftime("%H:%M:%S"))
    print()

    last_pct = -1
    try:
        while True:
            elapsed   = time.monotonic() - start
            remaining = duration - elapsed
            if remaining <= 0:
                break

            pct = int((elapsed / duration) * 100)
            if pct != last_pct:
                last_pct = pct
                print_status_tick("⏸ JEDA", remaining, duration, cycle,
                                  f"Lanjut {resume_dt.strftime('%H:%M:%S')}")

            time.sleep(1)

    finally:
        stats["total_pause"] += PAUSE_MINUTES * 60

    print(f"\r  ✅ Jeda selesai — {now_str()}{' '*30}", flush=True)

# ── Main ──────────────────────────────────────────────────
def main():
    # Tangani SIGTERM agar tidak crash di VPS
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    print_banner()

    stats = {
        "total_run"  : 0.0,
        "total_pause": 0.0,
        "total_lines": 0,
        "restarts"   : 0,
        "last_tick_run": -1,
    }

    cycle = 0
    start_global = time.monotonic()

    try:
        while True:
            cycle += 1
            run_phase(cycle, stats)
            pause_phase(cycle, stats)

            # Ringkasan kumulatif setelah tiap siklus lengkap
            uptime = time.monotonic() - start_global
            print()
            print("  ┄" * 20)
            print(f"  RINGKASAN  |  Siklus: {cycle}  |  Uptime: {fmt_dur_long(uptime)}")
            print(f"  Total jalan: {fmt_dur_long(stats['total_run'])}"
                  f"  |  Total jeda: {fmt_dur_long(stats['total_pause'])}")
            print(f"  Total log  : {stats['total_lines']} baris"
                  f"  |  Bot restart: {stats['restarts']}")
            print("  ┄" * 20 + "\n")

    except KeyboardInterrupt:
        uptime = time.monotonic() - start_global
        print(f"\n\n{'═'*62}")
        print("  Scheduler dihentikan (Ctrl+C)")
        print(f"{'═'*62}")
        print(f"  Siklus selesai : {cycle}")
        print(f"  Uptime total   : {fmt_dur_long(uptime)}")
        print(f"  Total jalan    : {fmt_dur_long(stats['total_run'])}")
        print(f"  Total jeda     : {fmt_dur_long(stats['total_pause'])}")
        print(f"  Total log      : {stats['total_lines']} baris")
        print(f"  Bot restart    : {stats['restarts']}")
        print(f"{'═'*62}\n")

if __name__ == "__main__":
    main()
