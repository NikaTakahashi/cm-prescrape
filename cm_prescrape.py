#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cm-prescrape — Pre-download Console Mode (MiSTer) artwork from your PC.

Replicates on the PC the same source used by Console Mode's default scraper
("Scrape Art (Default)"): the public libretro CDN https://thumbnails.libretro.com
and saves the artwork exactly where Console Mode expects to find it:

    <ROM folder>/media/<ROM name>.png        -> cover art
    <ROM folder>/media/<ROM name>-BG.png     -> background (screenshot)

Usage:
    python3 cm_prescrape.py              # interactive mode (menus)
    python3 cm_prescrape.py --all        # no questions: uses config.ini as-is
    python3 cm_prescrape.py --systems SNES,PSX --limit 5   # pilot test
"""

import argparse
import configparser
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

CDN = "https://thumbnails.libretro.com"
UA = "Mozilla/5.0 (X11; Linux x86_64) cm-prescrape/1.0"
SUBDIRS = ("boxarts", "snaps", "titles", "logos")
SUBDIR_PATH = {"boxarts": "Named_Boxarts", "snaps": "Named_Snaps",
               "titles": "Named_Titles", "logos": "Named_Logos"}

# Extensions that are NOT ROMs (ignored when scanning)
NON_ROM_EXTS = {
    ".cue", ".sbi", ".srm", ".m3u", ".m3u8", ".txt", ".xml", ".nfo", ".pdf",
    ".md", ".ini", ".png", ".jpg", ".jpeg", ".json", ".cfg", ".sav", ".sts",
    ".st0", ".st1", ".plg", ".pld", ".pl1", ".pl2", ".sram", ".bmd", ".m62",
    ".vfh", ".lst", ".bak", ".csv", ".log", ".url", ".db", ".tmp",
}

# MiSTer-style region tags -> libretro-style region tags
REGION_FIX = [
    ("(U)", "(USA)"), ("(E)", "(Europe)"), ("(J)", "(Japan)"),
    ("(W)", "(World)"), ("(M)", "(Prototype)"), ("(K)", "(Korea)"),
    ("(A)", "(Australia)"), ("(F)", "(France)"), ("(G)", "(Germany)"),
    ("(S)", "(Spain)"), ("(I)", "(Italy)"), ("(B)", "(Brazil)"),
    ("(C)", "(Canada)"), ("(D)", "(Netherlands)"), ("(Sw)", "(Sweden)"),
]

# Mapping games/<X> folder -> CDN folder (derived from the systems table
# embedded in ConsoleMode_arm + the real CDN index)
SYSTEM_MAP = {
    "3DO": "The 3DO Company - 3DO",
    "Amiga": "Commodore - Amiga",
    "Amstrad": "Amstrad - CPC",
    "Arcadia": "Emerson - Arcadia 2001",
    "Arduboy": "Arduboy Inc - Arduboy",
    "Atari2600": "Atari - 2600",
    "Atari5200": "Atari - 5200",
    "Atari7800": "Atari - 7800",
    "Atari800": "Atari - 8-bit",
    "AtariLynx": "Atari - Lynx",
    "AtariST": "Atari - ST",
    "AVision": "Entex - Adventure Vision",
    "C64": "Commodore - 64",
    "Casio_PV-1000": "Casio - PV-1000",
    "CD-i": "Philips - CD-i",
    "ChannelF": "Fairchild - Channel F",
    "Coleco": "Coleco - ColecoVision",
    "CreatiVision": "VTech - CreatiVision",
    "Game And Watch": "Handheld Electronic Game",
    "GameNWatch": "Handheld Electronic Game",
    "Gameboy": "Nintendo - Game Boy",
    "GAMEBOY2P": "Nintendo - Game Boy",
    "GameGear": "Sega - Game Gear",
    "GameGear2P": "Sega - Game Gear",
    "GBA": "Nintendo - Game Boy Advance",
    "GBA2P": "Nintendo - Game Boy Advance",
    "GBC": "Nintendo - Game Boy Color",
    "SGB": "Nintendo - Game Boy Color",
    "Intellivision": "Mattel - Intellivision",
    "Jaguar": "Atari - Jaguar",
    "Lynx48": "Atari - Lynx",
    "Mame": "MAME",
    "MegaCD": "Sega - Mega-CD - Sega CD",
    "MegaDrive": "Sega - Mega Drive - Genesis",
    "MSX": "Microsoft - MSX",
    "MSX1": "Microsoft - MSX",
    "N64": "Nintendo - Nintendo 64",
    "NeoGeo": "SNK - Neo Geo",
    "NeoGeo-CD": "SNK - Neo Geo CD",
    "NES": "Nintendo - Nintendo Entertainment System",
    "NGPC": "SNK - Neo Geo Pocket Color",
    "Odyssey2": "Magnavox - Odyssey2",
    "PC8801": "NEC - PC-8001 - PC-8801",
    "PET2001": "Commodore - PET",
    "PokemonMini": "Nintendo - Pokemon Mini",
    "PSX": "Sony - PlayStation",
    "S32X": "Sega - 32X",
    "Saturn": "Sega - Saturn",
    "SCV": "Epoch - Super Cassette Vision",
    "SG1000": "Sega - SG-1000",
    "SMS": "Sega - Master System - Mark III",
    "SNES": "Nintendo - Super Nintendo Entertainment System",
    "Spectrum": "Sinclair - ZX Spectrum",
    "ZXNext": "Sinclair - ZX Spectrum",
    "SPMX": "Sega - PICO",
    "Studio-II": "RCA - Studio II",
    "SuperVision": "Watara - Supervision",
    "SVI328": "Spectravideo - SVI-318 - SVI-328",
    "TGFX16": "NEC - PC Engine - TurboGrafx 16",
    "TGFX16-CD": "NEC - PC Engine CD - TurboGrafx-CD",
    "VECTREX": "GCE - Vectrex",
    "VIC20": "Commodore - VIC-20",
    "VirtualBoy": "Nintendo - Virtual Boy",
    "WonderSwan": "Bandai - WonderSwan",
    "WonderSwanColor": "Bandai - WonderSwan Color",
    "X68000": "Sharp - X68000",
    "ZX81": "Sinclair - ZX 81",
}


# ------------------------------------------------------------------ helpers
def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def normalize(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def http_get(url, timeout, retries):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1 + i)
    raise last


def parse_listing(html_bytes):
    txt = html_bytes.decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'href="([^"?]+)"', txt):
        href = m.group(1)
        if href.endswith(".png") and not href.startswith(("/", "?")):
            out.append(urllib.parse.unquote(href))
    return out


def ask(prompt, default):
    try:
        r = input(f"{prompt} [{default}]: ").strip()
        return r or default
    except EOFError:
        return default


# ------------------------------------------------------------ config / menus
def load_config(cfg_path):
    cfg = configparser.ConfigParser()
    read = cfg.read(cfg_path, encoding="utf-8")
    if not read:
        log(f"WARNING: could not read {cfg_path}; using default values.")
    def g(sec, key, default):
        try:
            return cfg.get(sec, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default
    return {
        "sd_root": g("path", "sd_root", "").strip(),
        "jobs": int(g("download", "jobs", "16")),
        "main_source": g("download", "main_source", "boxarts").lower(),
        "bg_source": g("download", "bg_source", "snaps").lower(),
        "systems": g("download", "systems", ""),
        "force": g("download", "force", "0") in ("1", "true", "yes"),
        "retries": int(g("network", "retries", "3")),
        "timeout": int(g("network", "timeout", "30")),
        "opt_enabled": g("optimize", "enabled", "1") in ("1", "true", "yes"),
        "opt_max_w": int(g("optimize", "max_width", "400")),
        "opt_max_h": int(g("optimize", "max_height", "400")),
    }


def menu_source(cfg, interactive):
    if not interactive:
        return cfg["main_source"], cfg["bg_source"], cfg["opt_enabled"]
    print("\n=== Cover art download source selection ===")
    print("  1) Box art (Named_Boxarts) + backgrounds (Named_Snaps)  [recommended, this is what Console Mode uses]")
    print("  2) Box art only")
    print("  3) Screenshots as covers (Named_Snaps)")
    print("  4) Title screens as covers (Named_Titles)")
    print("  5) Logos as covers (Named_Logos)")
    op = ask("Choose an option", "1")
    mapping = {
        "1": ("boxarts", "snaps"),
        "2": ("boxarts", "none"),
        "3": ("snaps", "none"),
        "4": ("titles", "none"),
        "5": ("logos", "none"),
    }
    fuente, fondo = mapping.get(op, mapping["1"])
    print("\nOptimized thumbnails: Console Mode reads its downscaled copies from")
    print("media/optimized/ (same as its own 'Optimize Artwork' option). Generate them")
    print("here so the MiSTer doesn't have to?")
    opt = ask("Generate optimized thumbnails too? (y/n)", "y" if cfg["opt_enabled"] else "n").lower()
    opt_enabled = opt in ("y", "yes")
    return fuente, fondo, opt_enabled


# ------------------------------------------------------------ CDN detection
def cdn_index(cache_dir, timeout, retries):
    """List of system folders available on the CDN (with local cache)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "__root.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    data = http_get(CDN + "/", timeout, retries)
    systems = []
    for m in re.finditer(r'href="([^"?/]+)/"', data.decode("utf-8", "replace")):
        systems.append(urllib.parse.unquote(m.group(1)))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(systems, f, ensure_ascii=False)
    return systems


def detect_platforms(games_root, cdn_systems):
    """Returns {mister_folder: cdn_folder} for systems with artwork coverage."""
    detection = {}
    norm_cdn = {normalize(s): s for s in cdn_systems}
    for folder in sorted(os.listdir(games_root)):
        path = os.path.join(games_root, folder)
        if not os.path.isdir(path) or folder == "media":
            continue
        cdn = SYSTEM_MAP.get(folder)
        if cdn and cdn in cdn_systems:
            detection[folder] = cdn
            continue
        # automatic match by normalized name
        n = normalize(folder)
        if n in norm_cdn:
            detection[folder] = norm_cdn[n]
    return detection


# ------------------------------------------------------------- CDN listings
def load_listing(cache_dir, cdn_folder, subdir, timeout, retries):
    os.makedirs(cache_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", cdn_folder)
    path = os.path.join(cache_dir, f"{safe}__{subdir}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    url = f"{CDN}/{urllib.parse.quote(cdn_folder)}/{SUBDIR_PATH[subdir]}/"
    try:
        names = parse_listing(http_get(url, timeout, retries))
    except Exception as e:  # noqa: BLE001
        log(f"  WARNING: no {subdir} listing for '{cdn_folder}' ({e})")
        names = []
    with open(path, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False)
    return names


# ------------------------------------------------------------------ scanning
def find_games_dir(sd_root):
    for name in ("games", "Games", "GAMES"):
        p = os.path.join(sd_root, name)
        if os.path.isdir(p):
            return p
    return None


def rom_bases_in(dirpath):
    bases = set()
    try:
        entries = os.listdir(dirpath)
    except OSError:
        return bases
    for e in entries:
        if e == "gamelist.xml":
            continue
        full = os.path.join(dirpath, e)
        if os.path.isdir(full):
            continue
        if os.path.splitext(e)[1].lower() in NON_ROM_EXTS:
            continue
        bases.add(os.path.splitext(e)[0])
    return bases


def scan_roms(games_root, detection, limit_per_system):
    """Yields jobs: (system_folder, rom_dir, basename)."""
    seen = set()
    for system, _cdn in sorted(detection.items()):
        syspath = os.path.join(games_root, system)
        n = 0
        for root, dirs, _files in os.walk(syspath):
            dirs[:] = sorted(d for d in dirs if d != "media")
            for base in sorted(rom_bases_in(root)):
                key = (system, os.path.relpath(root, syspath), base.lower())
                if key in seen:
                    continue
                seen.add(key)
                yield system, root, base
                n += 1
                if limit_per_system and n >= limit_per_system:
                    break
            if limit_per_system and n >= limit_per_system:
                break


# ------------------------------------------------------------- optimization
def optimize_image(src, dst, max_w, max_h):
    """Downscale src into dst (media/optimized/) like Console Mode's Optimize Artwork."""
    try:
        from PIL import Image
    except ImportError:
        return "no-pillow"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with Image.open(src) as im:
            im = im.convert("RGBA") if im.mode in ("P", "LA") else im
            im.thumbnail((max_w, max_h), Image.LANCZOS)
            im.save(dst, "PNG")
        return "ok"
    except Exception:  # noqa: BLE001
        return "fail"


def optimize_pass(games_root, max_w, max_h, force=False):
    """Walk every games/<system>/media folder and generate missing optimized thumbnails."""
    pend, done, failed = 0, 0, 0
    t0 = time.time()
    tasks = []
    for root, dirs, files in os.walk(games_root):
        if os.path.basename(root) != "media":
            continue
        dirs[:] = []  # do not descend into media/optimized
        opt_dir = os.path.join(root, "optimized")
        for f in sorted(files):
            if not f.lower().endswith(".png"):
                continue
            src = os.path.join(root, f)
            dst = os.path.join(opt_dir, f)
            if os.path.exists(dst) and not force:
                continue
            tasks.append((src, dst))
    total = len(tasks)
    log(f"Optimize pass: {total} thumbnails to generate ({max_w}x{max_h} max)...")
    for i, (src, dst) in enumerate(tasks):
        r = optimize_image(src, dst, max_w, max_h)
        if r == "ok":
            done += 1
        else:
            failed += 1
            if failed <= 5:
                log(f"  optimize error ({r}): {src}")
        if (i + 1) % 250 == 0:
            log(f"  optimize progress {i+1}/{total} ({time.time()-t0:.0f}s)")
    log(f"Optimize DONE in {time.time()-t0:.0f}s  generated={done}  failures={failed}")
    if failed and failed == total:
        log("NOTE: install Pillow to enable optimization:  pip install pillow")
    return total


def candidates(base, ci_index):
    """Tries the exact name and region variants. -> PNG name or None"""
    tried = set()

    def try_name(name):
        k = name.lower()
        if k in tried:
            return None
        tried.add(k)
        return ci_index.get(k + ".png")

    hit = try_name(base)
    if hit:
        return hit
    for src, dst in REGION_FIX:
        if base.endswith(src):
            hit = try_name(base[: -len(src)] + dst)
            if hit:
                return hit
    m = re.match(r"^(.*)\(([^()]+)\)$", base)
    if m:
        parts = m.group(2).split(",")
        if len(parts) > 1:
            for p in parts:
                hit = try_name(m.group(1) + "(" + p.strip() + ")")
                if hit:
                    return hit
            hit = try_name(m.group(1).rstrip())
    return hit


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Pre-download artwork for Console Mode")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini"))
    ap.add_argument("--all", action="store_true", help="no menus, use config.ini as-is")
    ap.add_argument("--sd", default="", help="SD root path (overrides config.ini)")
    ap.add_argument("--systems", default="", help="comma-separated systems")
    ap.add_argument("--limit", type=int, default=0, help="max games per system (test)")
    ap.add_argument("--jobs", type=int, default=0)
    ap.add_argument("--source", choices=SUBDIRS, default="", help="main artwork source")
    ap.add_argument("--bg", default="", choices=SUBDIRS + ("none",), help="background source")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--optimize-only", action="store_true",
                    help="only generate optimized thumbnails for already downloaded artwork")
    ap.add_argument("--no-optimize", action="store_true", help="skip optimized thumbnail generation")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.sd:
        cfg["sd_root"] = args.sd
    if args.jobs:
        cfg["jobs"] = args.jobs
    if args.systems:
        cfg["systems"] = args.systems

    interactive = sys.stdin.isatty() and not args.all
    source, background, opt_enabled = menu_source(cfg, interactive)
    if args.source:
        source = args.source
    if args.bg:
        background = args.bg
    if args.no_optimize:
        opt_enabled = False

    sd_root = cfg["sd_root"]
    if not sd_root or not os.path.isdir(sd_root):
        log(f"ERROR: sd_root path does not exist: '{sd_root}'")
        log("Edit config.ini and set 'sd_root' to the root of your MiSTer SD/SSD.")
        sys.exit(1)
    games_root = find_games_dir(sd_root)
    if not games_root:
        log(f"ERROR: could not find the 'games' folder inside {sd_root}")
        sys.exit(1)

    # --optimize-only: just generate optimized thumbnails and exit
    if args.optimize_only:
        optimize_pass(games_root, cfg["opt_max_w"], cfg["opt_max_h"], force=cfg["force"])
        return

    log(f"SD root      : {sd_root}")
    log(f"ROMs         : {games_root}")

    cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
    project = os.path.dirname(os.path.abspath(__file__))

    # 1) Systems available on the CDN + automatic platform detection
    log("Detecting platforms with artwork available (libretro CDN)...")
    try:
        cdn_systems = cdn_index(cache_dir, cfg["timeout"], cfg["retries"])
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: could not reach {CDN}: {e}")
        sys.exit(1)
    log(f"  {len(cdn_systems)} systems on the CDN")

    detection = detect_platforms(games_root, cdn_systems)
    log(f"{len(detection)} of your folders have artwork on the CDN:")
    for k in sorted(detection):
        log(f"  games/{k:20s} -> {detection[k]}")
    no_coverage = sorted(set(os.listdir(games_root)) - set(detection) - {"media"})
    log(f"No CDN coverage ({len(no_coverage)}): {', '.join(no_coverage[:12])}"
        + (" ..." if len(no_coverage) > 12 else ""))

    if interactive:
        r = ask("Continue with these systems? (y/n)", "y").lower()
        if r not in ("y", "yes"):
            sel = ask("Type the systems you want, comma-separated", "").strip()
            if sel:
                chosen = [s.strip() for s in sel.split(",") if s.strip() in detection]
                detection = {k: detection[k] for k in chosen}
                log(f"Selected systems: {', '.join(chosen)}")
            else:
                log("Cancelled.")
                sys.exit(0)
    if cfg["systems"].strip():
        filters = {s.strip() for s in cfg["systems"].split(",") if s.strip()}
        detection = {k: v for k, v in detection.items() if k in filters}
        log(f"config.ini filter active: {', '.join(sorted(detection))}")

    if not detection:
        log("No systems to process.")
        sys.exit(0)

    # 2) Load CDN listings per system and chosen source
    log(f"Loading listings (source={source}, background={background})...")
    listings = {}
    for system, cdn in sorted(detection.items()):
        t0 = time.time()
        main = load_listing(cache_dir, cdn, source, cfg["timeout"], cfg["retries"])
        bg_n = [] if background == "none" or background == source else \
            load_listing(cache_dir, cdn, background, cfg["timeout"], cfg["retries"])
        listings[system] = {
            "cdn": cdn,
            "main": {n.lower(): n for n in main},
            "bg": {n.lower(): n for n in bg_n},
        }
        log(f"  {system:20s} {len(main):6d} {source}, {len(bg_n):6d} {background}  ({time.time()-t0:.1f}s)")

    # 3) Scan ROMs and prepare download jobs
    log("Scanning ROMs and matching artwork...")
    jobs, total_roms, misses = [], 0, {}
    per_system = {}
    for system, rom_dir, base in scan_roms(games_root, detection, args.limit):
        total_roms += 1
        per_system[system] = per_system.get(system, 0) + 1
        info = listings[system]
        p = candidates(base, info["main"])
        b = candidates(base, info["bg"]) if info["bg"] else None
        if not p and not b:
            misses.setdefault(system, []).append(base)
            continue
        url_p = f"{CDN}/{urllib.parse.quote(info['cdn'])}/{SUBDIR_PATH[source]}/{urllib.parse.quote(p)}" if p else None
        url_b = f"{CDN}/{urllib.parse.quote(info['cdn'])}/{SUBDIR_PATH[background]}/{urllib.parse.quote(b)}" if b else None
        jobs.append((system, rom_dir, base, url_p, url_b))

    log(f"ROMs scanned: {total_roms}   with artwork available: {len(jobs)}   without artwork: {total_roms - len(jobs)}")
    if not jobs:
        log("Nothing to download.")
        sys.exit(0)

    # 4) Download
    log(f"Downloading {len(jobs)} games (threads={cfg['jobs']})...")
    ok = already = failed = 0
    t0 = time.time()
    err_lock = threading.Lock()
    err_samples = []

    def worker(job):
        system, rom_dir, base, url_p, url_b = job
        media = os.path.join(rom_dir, "media")
        res = []
        for url, suffix in ((url_p, ""), (url_b, "-BG")):
            if not url:
                continue
            dest = os.path.join(media, f"{base}{suffix}.png")
            if os.path.exists(dest) and not cfg["force"]:
                res.append("already")
                continue
            if args.dry_run:
                res.append("dry")
                continue
            try:
                os.makedirs(media, exist_ok=True)
                data = http_get(url, cfg["timeout"], cfg["retries"])
                if len(data) < 1000:
                    with err_lock:
                        if len(err_samples) < 5:
                            err_samples.append(f"tiny response ({len(data)}B): {url}")
                    res.append("fail")
                    continue
                tmp = dest + ".tmp"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, dest)
                res.append("ok")
                if opt_enabled:
                    optimize_image(dest,
                                   os.path.join(media, "optimized", f"{base}{suffix}.png"),
                                   cfg["opt_max_w"], cfg["opt_max_h"])
            except Exception as e:  # noqa: BLE001
                with err_lock:
                    if len(err_samples) < 5:
                        err_samples.append(f"{e}: {url}")
                res.append("fail")
        return res

    with ThreadPoolExecutor(max_workers=cfg["jobs"]) as ex:
        futures = [ex.submit(worker, t) for t in jobs]
        for i, fut in enumerate(as_completed(futures)):
            for r in fut.result():
                if r == "ok":
                    ok += 1
                elif r == "already":
                    already += 1
                elif r == "fail":
                    failed += 1
            if (i + 1) % 100 == 0:
                el = time.time() - t0
                log(f"  progress {i+1}/{len(jobs)}  new={ok} already={already} failed={failed} ({el:.0f}s)")

    # 5) Report (ConsoleMode/ScrapeLogs style)
    el = time.time() - t0
    log(f"DONE in {el:.0f}s  downloaded={ok}  already_existed={already}  failures={failed}")
    if err_samples:
        log("Sample errors:")
        for e in err_samples:
            log(f"  - {e}")
    report = os.path.join(project, "download_report.csv")
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "roms", "with_artwork", "without_artwork"])
        for s in sorted(per_system):
            without = len(misses.get(s, []))
            w.writerow([s, per_system[s], per_system[s] - without, without])
    log(f"Per-system report: {report}")

    if misses:
        missing = os.path.join(project, "no_artwork.csv")
        with open(missing, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["system", "rom"])
            for s in sorted(misses):
                for b in misses[s]:
                    w.writerow([s, b])
        log(f"Games without artwork on the CDN: {missing}")

    # 6) Optimized thumbnails (Console Mode's media/optimized/)
    if opt_enabled:
        optimize_pass(games_root, cfg["opt_max_w"], cfg["opt_max_h"], force=cfg["force"])
    else:
        log("Optimized thumbnails skipped (--no-optimize). Console Mode will generate them on device.")


if __name__ == "__main__":
    main()
