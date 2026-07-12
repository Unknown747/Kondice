#!/usr/bin/env python3
"""
Stake Dice Bot — Auto Scheduler
Siklus: Jalan 30 menit → Jeda 10 menit → Ulangi tak terbatas

Jalankan: python scheduler.py
Hentikan: Ctrl+C
"""
from __future__ import annotations

import subprocess, sys, os, time, threading, signal, requests
from datetime import datetime, timedelta

# ── Konfigurasi siklus ────────────────────────────────────
RUN_MINUTES   = 30    # durasi bot aktif per siklus
PAUSE_MINUTES = 10    # durasi jeda antar siklus
BOT_SCRIPT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dice_bot.py")

# ── Stake API ─────────────────────────────────────────────
API_URL  = "https://stake.com/_api/graphql"

def _load_api_key() -> str:
    key = os.environ.get("STAKE_API_KEY", "").strip()
    if not key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("STAKE_API_KEY=") and not line.startswith("#"):
                        key = line.split("=", 1)[1].strip()
                        break
    return key

API_KEY  = _load_api_key()

# Urutan naik flag VIP di Stake
VIP_FLAGS = ["bronze", "silver", "gold", "platinum", "diamond", "obsidian", "master"]
VIP_LABEL = {
    "bronze"  : "🥉 Bronze",
    "silver"  : "🥈 Silver",
    "gold"    : "🥇 Gold",
    "platinum": "💎 Platinum",
    "diamond" : "💠 Diamond",
    "obsidian": "🖤 Obsidian",
    "master"  : "👑 Master",
}

_api_session = requests.Session()
_api_session.headers.update({
    "Content-Type"                : "application/json",
    "Accept"                      : "*/*",
    "Accept-Language"             : "en-US,en;q=0.9",
    "Accept-Encoding"             : "gzip, deflate",
    "Origin"                      : "https://stake.com",
    "Referer"                     : "https://stake.com/",
    "User-Agent"                  : (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "x-access-token"              : API_KEY,
    "x-language"                  : "en",
    "apollographql-client-name"   : "web",
    "apollographql-client-version": "1.0.0",
    "Connection"                  : "keep-alive",
})

STATS_QUERY = """
query SchedulerStats {
  user {
    name
    flagProgress { progress flag }
    statistic { amount currency game }
    balances { available { amount currency } }
  }
}
"""

def fetch_stake_stats() -> dict | None:
    """Ambil VIP progress + total wager IDR dari API Stake. Return None jika gagal."""
    try:
        r = _api_session.post(API_URL, json={"query": STATS_QUERY}, timeout=15)
        if r.status_code != 200 or not r.text:
            return None
        d = r.json()
        if "errors" in d:
            return None
        u = d.get("data", {}).get("user", {})

        # VIP flag progress
        fp = u.get("flagProgress") or {}
        flag     = fp.get("flag", "?").lower()
        progress = float(fp.get("progress", 0))

        # Next flag label
        try:
            idx      = VIP_FLAGS.index(flag)
            next_flag = VIP_FLAGS[idx + 1] if idx + 1 < len(VIP_FLAGS) else None
        except ValueError:
            next_flag = None

        # Total wager IDR (statistic array, filter currency=idr, game=dice)
        total_idr = 0.0
        for st in u.get("statistic", []):
            if st.get("currency") == "idr" and st.get("game") == "dice":
                total_idr = float(st.get("amount", 0))
                break

        balance_idr = 0.0
        for b in u.get("balances", []):
            avail = b.get("available", {})
            if avail.get("currency") == "idr":
                balance_idr = float(avail.get("amount", 0))
                break

        return {
            "name"       : u.get("name", "?"),
            "flag"       : flag,
            "progress"   : progress,
            "next_flag"  : next_flag,
            "total_idr"  : total_idr,
            "balance_idr": balance_idr,
        }
    except Exception:
        return None

# ── Helpers display ───────────────────────────────────────
W           = 62        # lebar box
IDR_PER_USD = 16_000.0  # estimasi kurs untuk tampilan USD

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

def fmt_rp(amount: float) -> str:
    """Format angka → Rp 1.234.567,89"""
    return f"Rp {amount:>15,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def progress_bar(pct: float, width: int = 22) -> str:
    """0-100 → ████░░░░░░"""
    done = int(pct / 100 * width)
    return "█" * done + "░" * (width - done)

def box_line(text: str = "") -> str:
    return "│  " + text.ljust(W - 4) + "│"

def box_sep():
    return "├" + "─" * W + "┤"

def print_banner():
    print("\n" + "╔" + "═" * W + "╗")
    print("║" + "  STAKE DICE BOT — AUTO SCHEDULER".center(W) + "║")
    print("║" + f"  ▶ Jalan {RUN_MINUTES} mnt  →  ⏸ Jeda {PAUSE_MINUTES} mnt  →  ♻ Ulangi".center(W) + "║")
    print("║" + "  Ctrl+C untuk berhenti kapanpun".center(W) + "║")
    print("╚" + "═" * W + "╝\n")

def print_run_header(cycle, start_at, stop_at):
    print("┌" + "─" * W + "┐")
    print("│" + f"  ▶  SIKLUS #{cycle}  —  BOT JALAN".ljust(W) + "│")
    print("│" + f"  Mulai : {start_at}   |   Stop  : {stop_at}   |   Durasi: {RUN_MINUTES} mnt".ljust(W) + "│")
    print("└" + "─" * W + "┘")

def print_pause_header(cycle, start_at, resume_at):
    print("\n" + "┌" + "─" * W + "┐")
    print("│" + f"  ⏸  SIKLUS #{cycle}  —  BOT DIJEDA".ljust(W) + "│")
    print("│" + f"  Mulai jeda : {start_at}   |   Lanjut : {resume_at}".ljust(W) + "│")
    print("└" + "─" * W + "┘")

def print_status_tick(label, remaining, total, extra=""):
    pct  = int((1 - remaining / total) * 100)
    done = int(pct / 100 * 22)
    bar  = "█" * done + "░" * (22 - done)
    print(f"\r  {label} [{bar}] {fmt_dur(remaining)} sisa  ({pct}%)  {extra}  ",
          end="", flush=True)

def print_vip_wager_report(cycle: int, stats_before: dict | None, stats_after: dict | None,
                            wager_this_cycle: float, wager_today: float,
                            elapsed_run: float, line_count: int, restarts: int,
                            saldo_awal: float, saldo_sekarang: float):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "═" * (W + 2))
    print("│  " + f"📊  LAPORAN SIKLUS #{cycle}  —  {ts}".ljust(W) + "│")
    print(box_sep())

    # ── Ringkasan run ──────────────────────────────────────
    print(box_line("  RINGKASAN RUN"))
    print(box_line(f"  Durasi jalan   : {fmt_dur_long(elapsed_run)}"))
    print(box_line(f"  Baris log      : {line_count:,}  |  Restart bot: {restarts}"))
    print(box_sep())

    # ── VIP + Wager ────────────────────────────────────────
    print(box_line("  VIP STATUS & AKUMULASI WAGER HARI INI"))
    if stats_after:
        flag    = stats_after["flag"]
        prog    = stats_after["progress"] * 100
        nxt     = stats_after["next_flag"]
        lbl_cur = VIP_LABEL.get(flag, flag.capitalize())

        if stats_before and stats_before["flag"] != stats_after["flag"]:
            lbl_bef   = VIP_LABEL.get(stats_before["flag"], stats_before["flag"].capitalize())
            level_str = f"{lbl_bef}  -->  {lbl_cur}"
        else:
            level_str = lbl_cur

        bar = progress_bar(prog, 24)
        print(box_line(f"  Level saat ini : {level_str}"))
        print(box_line(f"  Progress EXP   : [{bar}]  {prog:.2f}%"))
    else:
        print(box_line("  (Gagal ambil data VIP dari API)"))

    usd = wager_today / IDR_PER_USD
    print(box_line(f"  Wager Siklus Ini: {fmt_rp(wager_this_cycle)}"))
    print(box_line(f"  TOTAL WAGER HARI INI (IDR) : {fmt_rp(wager_today)}"))
    print(box_line(f"  TOTAL WAGER HARI INI (USD) : $ {usd:>10,.2f}  (Estimasi)"))
    print(box_sep())

    # ── Kondisi finansial ──────────────────────────────────
    print(box_line("  KONDISI FINANSIAL AKUN"))
    if saldo_awal > 0 and saldo_sekarang > 0:
        net  = saldo_sekarang - saldo_awal
        pct  = net / saldo_awal * 100
        sign = "+" if net >= 0 else ""
        net_idn = f"{abs(net):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        print(box_line(f"  Saldo Awal Hari Ini : {fmt_rp(saldo_awal)}"))
        print(box_line(f"  Saldo Saat Ini      : {fmt_rp(saldo_sekarang)}"))
        print(box_line(f"  Net Untung Bersih   : Rp {sign}{net_idn}  ({sign}{pct:.2f}%)"))
    else:
        print(box_line("  (Data saldo tidak tersedia)"))

    print("└" + "─" * W + "┘\n")

# ── Output forwarder ──────────────────────────────────────
def forward_output(proc, stop_event, line_count):
    """Thread: baca stdout bot dan cetak ke terminal."""
    for line in proc.stdout:
        if stop_event.is_set():
            break
        print(line, end="", flush=True)
        line_count[0] += 1

# ── Fase JALAN ────────────────────────────────────────────
def run_phase(cycle: int, stats: dict, wager_today: float) -> tuple[float, float]:
    """
    Jalankan bot selama RUN_MINUTES menit.
    Return (elapsed_seconds, wager_this_cycle).
    """
    duration = RUN_MINUTES * 60
    start    = time.monotonic()
    start_dt = datetime.now()
    stop_dt  = start_dt + timedelta(seconds=duration)

    print_run_header(cycle, start_dt.strftime("%H:%M:%S"), stop_dt.strftime("%H:%M:%S"))

    # Reset ticker agar tidak miss 5-menit pertama siklus baru
    stats["last_tick_run"] = -1

    # Snapshot VIP + wager SEBELUM run
    print(f"  [{now_str()}]  ⟳  Mengambil data VIP & wager awal...", flush=True)
    stats_before = fetch_stake_stats()
    if stats_before:
        label = VIP_LABEL.get(stats_before["flag"], stats_before["flag"].capitalize())
        prog  = stats_before["progress"] * 100
        print(f"  [{now_str()}]  ✓  VIP: {label}  |  EXP: {prog:.2f}%  |  "
              f"Total wager: {fmt_rp(stats_before['total_idr'])}\n", flush=True)
        # Rekam saldo awal hari ini hanya sekali (siklus pertama)
        if stats.get("saldo_awal_hari", 0.0) == 0.0 and stats_before.get("balance_idr", 0) > 0:
            stats["saldo_awal_hari"] = stats_before["balance_idr"]
    else:
        print(f"  [{now_str()}]  ⚠  Gagal ambil data awal (akan dicoba lagi di akhir)\n", flush=True)

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
                    restarts += 1
                    stop_event.set()
                    stop_event = threading.Event()
                    print(f"\n  ─── [{now_str()}]  Bot berhenti sendiri — restart #{restarts} ───\n",
                          flush=True)

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
                resume = datetime.now() + timedelta(seconds=remaining)
                print(f"\n  [⏱  {now_str()}  |  Sisa jalan: {fmt_dur(remaining)}"
                      f"  |  Siklus #{cycle}  |  Log: {line_count[0]:,} baris"
                      f"  |  Stop: {resume.strftime('%H:%M:%S')}]\n", flush=True)

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

    # Snapshot VIP + wager SETELAH run
    print(f"\n  [{now_str()}]  ⟳  Mengambil data VIP & wager akhir...", flush=True)
    time.sleep(2)   # beri jeda kecil agar Stake sempat update statistik
    stats_after    = fetch_stake_stats()
    saldo_sekarang = stats_after.get("balance_idr", 0.0) if stats_after else 0.0

    # Hitung wager siklus ini
    wager_this_cycle = 0.0
    if stats_before and stats_after:
        wager_this_cycle = max(0.0, stats_after["total_idr"] - stats_before["total_idr"])
    elif stats_after:
        wager_this_cycle = 0.0   # tidak bisa hitung delta

    wager_today_new = wager_today + wager_this_cycle

    # Perbarui akumulator global
    stats["total_run"]    += elapsed_real
    stats["total_lines"]  += line_count[0]
    stats["restarts"]     += restarts

    # Cetak laporan
    print_vip_wager_report(
        cycle            = cycle,
        stats_before     = stats_before,
        stats_after      = stats_after,
        wager_this_cycle = wager_this_cycle,
        wager_today      = wager_today_new,
        elapsed_run      = elapsed_real,
        line_count       = line_count[0],
        restarts         = restarts,
        saldo_awal       = stats.get("saldo_awal_hari", 0.0),
        saldo_sekarang   = saldo_sekarang,
    )

    return elapsed_real, wager_today_new

# ── Fase JEDA ─────────────────────────────────────────────
def pause_phase(cycle: int, stats: dict):
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
                print_status_tick("⏸ JEDA", remaining, duration,
                                  f"Lanjut {resume_dt.strftime('%H:%M:%S')}")
            time.sleep(1)

    finally:
        # Gunakan elapsed aktual, bukan fixed PAUSE_MINUTES (bisa di-Ctrl+C lebih awal)
        actual_pause = time.monotonic() - start
        stats["total_pause"] += actual_pause

    print(f"\r  ✅  Jeda selesai — {now_str()}{' ' * 35}", flush=True)

# ── Main ──────────────────────────────────────────────────
def main():
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    if not API_KEY:
        print("⚠  STAKE_API_KEY tidak ditemukan — data VIP/wager tidak tersedia\n")

    print_banner()

    stats = {
        "total_run"      : 0.0,
        "total_pause"    : 0.0,
        "total_lines"    : 0,
        "restarts"       : 0,
        "last_tick_run"  : -1,
        "saldo_awal_hari": 0.0,
    }

    wager_today   = 0.0
    cycle         = 0
    start_global  = time.monotonic()

    try:
        while True:
            cycle += 1

            # ── Run ───────────────────────────────────────
            elapsed_run, wager_today = run_phase(cycle, stats, wager_today)

            # ── Pause ─────────────────────────────────────
            pause_phase(cycle, stats)

            # Ringkasan kumulatif
            uptime = time.monotonic() - start_global
            print()
            print("  " + "┄" * 31)
            print(f"  TOTAL  |  Siklus selesai: {cycle}  |  Uptime: {fmt_dur_long(uptime)}")
            print(f"  Jalan: {fmt_dur_long(stats['total_run'])}  "
                  f"|  Jeda: {fmt_dur_long(stats['total_pause'])}  "
                  f"|  Wager hari ini: {fmt_rp(wager_today)}")
            print("  " + "┄" * 31 + "\n")

    except KeyboardInterrupt:
        uptime = time.monotonic() - start_global
        print(f"\n\n{'═' * W}")
        print("  Scheduler dihentikan (Ctrl+C)")
        print(f"{'═' * W}")
        print(f"  Siklus selesai : {cycle}")
        print(f"  Uptime total   : {fmt_dur_long(uptime)}")
        print(f"  Total jalan    : {fmt_dur_long(stats['total_run'])}")
        print(f"  Total jeda     : {fmt_dur_long(stats['total_pause'])}")
        print(f"  Total log      : {stats['total_lines']:,} baris")
        print(f"  Bot restart    : {stats['restarts']}")
        print(f"  Wager hari ini : {fmt_rp(wager_today)}")
        print(f"{'═' * W}\n")

if __name__ == "__main__":
    main()
