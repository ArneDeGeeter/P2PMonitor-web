#!/usr/bin/env python3
"""OSRS Player Guesser — identify a ranked player from their stat levels."""

import asyncio
import argparse
import math
import socket
from urllib.parse import urlencode
from playwright.async_api import async_playwright, Page as BrowserPage, Browser
from bs4 import BeautifulSoup

try:
    from stem import Signal
    from stem.control import Controller as TorController
    HAS_STEM = True
except ImportError:
    HAS_STEM = False

SKILLS = [
    "Overall", "Attack", "Defence", "Strength", "Hitpoints", "Ranged",
    "Prayer", "Magic", "Cooking", "Woodcutting", "Fletching", "Fishing",
    "Firemaking", "Crafting", "Smithing", "Mining", "Herblore", "Agility",
    "Thieving", "Slayer", "Farming", "Runecraft", "Hunter", "Construction",
    "Sailing",
]
NUM_SKILLS = len(SKILLS)

BASE = "https://secure.runescape.com/m=hiscore_oldschool"
RANKED_URL = f"{BASE}/overall.ws"
STATS_URL = f"{BASE}/index_lite.ws"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

TOR_SOCKS_PORT  = 9050
TOR_CONTROL_PORT = 9051
PARALLEL_PAGES  = 50    # number of parallel browser contexts (each its own Tor circuit)

CONCURRENCY_STATS = 300
PAGE_DELAY       = 0.3
PAGE_RETRY_WAIT  = 8.0
MAX_RETRIES      = 4
DEFAULT_MAX      = 1_000_000


# ---------------------------------------------------------------------------
# Tor helpers
# ---------------------------------------------------------------------------

def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _rotate_tor_circuit() -> None:
    """Ask Tor for a new set of circuits via the control port."""
    if not HAS_STEM:
        return
    try:
        with TorController.from_port(port=TOR_CONTROL_PORT) as ctrl:
            ctrl.authenticate()
            ctrl.signal(Signal.NEWNYM)
        print("  [tor] new circuit requested", flush=True)
    except Exception as e:
        print(f"  [tor] circuit rotation failed: {e}", flush=True)


# ---------------------------------------------------------------------------
# Hiscores page helpers
# ---------------------------------------------------------------------------

def _parse_ranked_page(html: str) -> list[tuple[str, int, int]]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr", class_="personal-hiscores__row"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        try:
            rank  = int(tds[1].get_text(strip=True).replace(",", ""))
            name  = tds[2].get_text(strip=True)
            level = int(tds[3].get_text(strip=True).replace(",", ""))
            rows.append((name, rank, level))
        except (ValueError, AttributeError):
            continue
    return rows


async def _navigate(pw_page: BrowserPage, params: dict, timeout_ms: int = 15_000) -> str:
    url = f"{RANKED_URL}?{urlencode(params)}"
    await pw_page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
    return await pw_page.content()


async def fetch_ranked_page(pw_page: BrowserPage, skill_idx: int, page: int) -> list[tuple[str, int, int]]:
    params = {"table": skill_idx, "page": page}
    wait = PAGE_RETRY_WAIT
    for _ in range(MAX_RETRIES):
        try:
            html = await _navigate(pw_page, params)
        except asyncio.TimeoutError:
            print(f"    [timeout] page {page}, waiting {wait:.0f}s…", flush=True)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        except Exception as e:
            print(f"    [error] page {page}: {e}, waiting {wait:.0f}s…", flush=True)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
            continue
        rows = _parse_ranked_page(html)
        if rows:
            return rows
        print(f"    [empty] page {page}, waiting {wait:.0f}s…", flush=True)
        await asyncio.sleep(wait)
        wait = min(wait * 2, 60)
    return []


MAX_PAGE = 80_000


async def _find_lower_bound(pw_page: BrowserPage, skill_idx: int, level: int) -> int:
    """Binary-search for the first page whose minimum level ≤ target."""
    lo, hi, first = 1, MAX_PAGE, MAX_PAGE
    print("  [binary search] finding lower bound…", flush=True)
    while lo <= hi:
        mid = (lo + hi) // 2
        rows = await fetch_ranked_page(pw_page, skill_idx, mid)
        await asyncio.sleep(PAGE_DELAY)
        if not rows:
            print(f"    page {mid:>6}: empty  → left  [lo={lo} hi={hi-1}]", flush=True)
            hi = mid - 1
            continue
        lvl_range = f"{min(r[2] for r in rows)}–{max(r[2] for r in rows)}"
        if min(r[2] for r in rows) <= level:
            print(f"    page {mid:>6}: {lvl_range}  → first≤{level}, left  [lo={lo} hi={hi-1}]", flush=True)
            first = mid
            hi = mid - 1
        else:
            print(f"    page {mid:>6}: {lvl_range}  → all>{level}, right  [lo={mid+1} hi={hi}]", flush=True)
            lo = mid + 1
    return first


async def _scan_forward(pw_pages: list[BrowserPage], skill_idx: int,
                         level: int, first_page: int) -> list[str]:
    """Scan forward from first_page with parallel workers, collecting names at exactly
    `level`.  Stops when a page is empty or its maximum level drops below `level`."""
    names: list[str] = []
    lock  = asyncio.Lock()
    state = {"next": first_page, "stop": False, "fetched": 0}

    async def worker(pw_page: BrowserPage) -> None:
        while True:
            async with lock:
                if state["stop"]:
                    return
                page_num = state["next"]
                state["next"] += 1

            rows = await fetch_ranked_page(pw_page, skill_idx, page_num)
            await asyncio.sleep(PAGE_DELAY)

            async with lock:
                state["fetched"] += 1
                if not rows or max(r[2] for r in rows) < level:
                    state["stop"] = True
                    reason = "empty" if not rows else f"max level {max(r[2] for r in rows) if rows else '?'} < {level}"
                    print(f"  page {page_num}: {reason} → end of range", flush=True)
                    return
                added = [n for n, _, lvl in rows if lvl == level]
                names.extend(added)
                print(f"  page {page_num}: {len(added)} matches, {len(names)} total", flush=True)

    await asyncio.gather(*[worker(pg) for pg in pw_pages])
    return names


# ---------------------------------------------------------------------------
# Individual stat lookup  (aiohttp — not rate-limited)
# ---------------------------------------------------------------------------

def _parse_stats_text(text: str) -> list[int] | None:
    """Parse the CSV body returned by index_lite.ws into a list of NUM_SKILLS levels."""
    lines = text.strip().split("\n")
    if len(lines) < 3:
        return None  # too short — probably a redirect page or error
    levels: list[int] = []
    for line in lines[:NUM_SKILLS]:
        parts = line.strip().split(",")
        try:
            lvl = int(parts[1]) if len(parts) >= 2 else 1
            levels.append(max(lvl, 1))
        except (ValueError, IndexError):
            levels.append(1)
    while len(levels) < NUM_SKILLS:
        levels.append(1)
    return levels


async def fetch_player_stats(pw_page: BrowserPage, name: str,
                             retries: int = 4) -> list[int] | None:
    url = f"{STATS_URL}?player={name}"
    wait = PAGE_RETRY_WAIT
    for attempt in range(retries):
        try:
            # Use the browser's fetch() so the request shares the page's Tor
            # circuit and CF cookies without navigating away from the current page.
            text: str = await pw_page.evaluate(
                "(url) => fetch(url).then(r => r.text())", url
            )
        except Exception as e:
            print(f"    [stats] {name} attempt {attempt+1}/{retries}: {type(e).__name__} — retrying in {wait:.0f}s", flush=True)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 30)
            continue
        result = _parse_stats_text(text)
        if result is not None:
            return result
        print(f"    [stats] {name} attempt {attempt+1}/{retries}: bad response ({text[:60]!r}) — retrying in {wait:.0f}s", flush=True)
        await asyncio.sleep(wait)
        wait = min(wait * 2, 30)
    return None


async def _fetch_all_stats(pw_pages: list[BrowserPage], names: list[str]) -> dict[str, list[int]]:
    """Fetch stats for all names using the shared Playwright page pool."""
    page_queue: asyncio.Queue[BrowserPage] = asyncio.Queue()
    for pg in pw_pages:
        await page_queue.put(pg)

    total = len(names)
    done  = 0
    results: dict[str, list[int]] = {}
    lock  = asyncio.Lock()

    async def one(name: str) -> None:
        nonlocal done
        pg = await page_queue.get()
        try:
            stats = await fetch_player_stats(pg, name)
        finally:
            await page_queue.put(pg)
        async with lock:
            done += 1
            results[name] = stats if stats is not None else [0] * NUM_SKILLS
            if done % 100 == 0 or done == total:
                print(f"  stats {done}/{total}…", flush=True)

    await asyncio.gather(*[one(n) for n in names])
    return results


# ---------------------------------------------------------------------------
# Pool construction
# ---------------------------------------------------------------------------

MAX_PAGES = 40_000


async def build_pool(pw_pages: list[BrowserPage],
                     skill_idx: int, level: int, max_cands: int,
                     output_file: str | None = None) -> tuple[dict | None, int]:
    print(f"\nSearching for players with {SKILLS[skill_idx]} level {level}…")
    bsearch_eta = int(math.log2(MAX_PAGE) * (PAGE_DELAY + 1))
    print(f"Finding lower bound (binary search, ~{bsearch_eta}s)…")
    first_page = await _find_lower_bound(pw_pages[0], skill_idx, level)

    # Rotate circuits so parallel workers start with fresh exit IPs
    _rotate_tor_circuit()

    print(f"Scanning forward from page {first_page} ({PARALLEL_PAGES} workers)…")
    names = await _scan_forward(pw_pages, skill_idx, level, first_page)
    print(f"Found {len(names):,} players with exactly {SKILLS[skill_idx]} {level}.")

    if len(names) > max_cands:
        return None, len(names)

    if output_file:
        with open(output_file, "w") as f:
            f.writelines(f"{n}\n" for n in names)
        print(f"Names written to {output_file}")

    print(f"Fetching full stats for {len(names):,} players (concurrent)…")
    candidates = await _fetch_all_stats(pw_pages, names)
    print(f"Pool ready: {len(candidates):,} candidates.\n")
    return candidates, len(candidates)


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def _entropy(values: list[int]) -> float:
    from collections import Counter
    total = len(values)
    return -sum((c / total) * math.log2(c / total) for c in Counter(values).values())


def best_stat(candidates: dict[str, list[int]], asked: set[int]) -> int | None:
    best_idx, best_ent = None, -1.0
    for i in range(NUM_SKILLS):
        if i in asked:
            continue
        vals = [s[i] for s in candidates.values()]
        if len(set(vals)) <= 1:
            continue
        e = _entropy(vals)
        if e > best_ent:
            best_ent, best_idx = e, i
    if best_idx is None:
        for i in range(NUM_SKILLS):
            if i not in asked:
                return i
    return best_idx


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _print_table(candidates: dict[str, list[int]], asked: set[int]) -> None:
    """Print remaining candidates with their revealed stat levels as a table."""
    asked_list = sorted(asked)
    headers = ["Name"] + [SKILLS[i] for i in asked_list]
    rows = [
        [name] + [str(stats[i]) for i in asked_list]
        for name, stats in sorted(candidates.items())
    ]
    widths = [max(len(h), max((len(r[j]) for r in rows), default=0))
              for j, h in enumerate(headers)]
    sep = "─" * (sum(widths) + 3 * len(widths) - 1)
    def fmt(row: list[str]) -> str:
        return "  │  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row))
    print(fmt(headers))
    print(sep)
    for row in rows:
        print(fmt(row))
    print()


def prompt_skill() -> int:
    print("Which stat do you want to reveal?")
    for i, skill in enumerate(SKILLS):
        print(f"  {i:2d}. {skill}")
    while True:
        raw = input(f"Enter number (0–{NUM_SKILLS - 1}): ").strip()
        try:
            idx = int(raw)
            if 0 <= idx < NUM_SKILLS:
                return idx
        except ValueError:
            pass
        print(f"Please enter a number between 0 and {NUM_SKILLS - 1}.")


def prompt_level(skill_name: str, skill_idx: int) -> int:
    max_lv = 2376 if skill_idx == 0 else 99
    while True:
        raw = input(f"What is their {skill_name} level (1–{max_lv})? ").strip()
        try:
            lvl = int(raw)
            if 1 <= lvl <= max_lv:
                return lvl
        except ValueError:
            pass
        print(f"Please enter a level between 1 and {max_lv}.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _make_pages(browser: Browser, n: int, proxy: dict | None) -> list[BrowserPage]:
    pages = []
    for _ in range(n):
        ctx  = await browser.new_context(proxy=proxy)
        page = await ctx.new_page()
        pages.append(page)
    return pages


async def run(max_cands: int, parallel: int, output_file: str | None) -> None:
    print("=== OSRS Player Guesser ===")
    print("Think of a ranked OSRS player — I'll figure out who they are!\n")
    print("Tip: start with a rare/high skill (Herblore, Slayer, Construction, Farming…)\n")

    # Check Tor
    if not _is_port_open(TOR_SOCKS_PORT):
        print(f"Tor SOCKS proxy not found on port {TOR_SOCKS_PORT}.")
        print("Start it with:  brew services start tor   (or just: tor)")
        return
    proxy = {"server": f"socks5://127.0.0.1:{TOR_SOCKS_PORT}"}
    print(f"Tor detected on port {TOR_SOCKS_PORT}.", flush=True)
    if not HAS_STEM:
        print("  (install stem for circuit rotation: pip install stem)", flush=True)

    print(f"Starting browser with {parallel} parallel contexts…", flush=True)
    async with async_playwright() as pw:
        browser  = await pw.chromium.launch(headless=True)
        pw_pages = await _make_pages(browser, parallel, proxy)

        # Warm up: verify page 1 loads through Tor
        print("Verifying connection through Tor…", flush=True)
        try:
            html = await _navigate(pw_pages[0], {"table": 0, "page": 1}, timeout_ms=30_000)
            if not _parse_ranked_page(html):
                print("Connected but got no rows — hiscores may be down.")
                return
        except Exception as e:
            print(f"Could not reach hiscores through Tor: {e}")
            return
        print("Ready.\n", flush=True)

        candidates: dict[str, list[int]] | None = None
        seed_idx = -1
        while candidates is None:
            seed_idx = prompt_skill()
            level    = prompt_level(SKILLS[seed_idx], seed_idx)
            candidates, count = await build_pool(pw_pages, seed_idx, level, max_cands, output_file)
            if candidates is None:
                print(f"\n~{count:,} slots exceed the {MAX_PAGES}-page limit.")
                print("(Use --max-pages N to raise it, or pick a rarer stat.)\n")
            elif len(candidates) == 0:
                print("No ranked players found. Try a different stat.")
                candidates = None

        asked = {seed_idx}
        while len(candidates) > 1:
            print(f"\n{len(candidates)} candidates remain.")
            if len(candidates) < 100:
                _print_table(candidates, asked)
            stat_idx = prompt_skill()
            level    = prompt_level(SKILLS[stat_idx], stat_idx)
            asked.add(stat_idx)
            candidates = {n: s for n, s in candidates.items() if s[stat_idx] == level}
            if len(candidates) == 0:
                print("\nNo match — that level doesn't fit any candidate.")
                print("The player may be unranked or have their account set to private.")
                return

        if len(candidates) == 1:
            print(f"\nThe player is: {next(iter(candidates))}")
        else:
            print(f"\nCouldn't narrow to one player. {len(candidates)} remain:")
            for name in candidates:
                print(f"  {name}")


def main() -> None:
    global MAX_PAGES, PARALLEL_PAGES
    parser = argparse.ArgumentParser(description="Identify an OSRS player from their stat levels.")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help=f"Max hiscores pages to scan (default: {MAX_PAGES:,})")
    parser.add_argument("--parallel", type=int, default=PARALLEL_PAGES,
                        help=f"Parallel Tor browser contexts (default: {PARALLEL_PAGES})")
    parser.add_argument("--output", metavar="FILE",
                        help="Write candidate names to FILE after the initial fetch")
    args = parser.parse_args()
    MAX_PAGES      = args.max_pages
    PARALLEL_PAGES = args.parallel
    asyncio.run(run(args.max_candidates, args.parallel, args.output))


if __name__ == "__main__":
    main()
