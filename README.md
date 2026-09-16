---
type: Readme
title: taq
description: A terminal HUD for the four things that are true about your machine right now:.
status: active
created: 2026-09-17
timestamp: 2026-09-17
tags: []
---

# taq

A terminal HUD for the four things that are true about your machine right now:
how much Claude quota is left, whether your traffic is actually going where you
think it is, what Docker is doing, and what the system is doing.

Stdlib Python, no dependencies, ~26 MB resident.

```
 taq   1 Claude   2 Docker   3 System   4 Coherence                          ◆ 1/2   ▲ COHERENT   09:53
╭─ Claude ─────────────────────────────────────────────────╮╭─ System ─────────────────────────────────╮
│ Plan usage limits Max (5x)                               ││ CPU   7.6%  51°C   load 1.32             │
│                                                          ││ ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀ │
│ Current session                                13% used  ││ ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣀⣀⣀ │
│ ███▌░░░░░░░░░░░░░░░░░░░░░░░░░░                           ││  0  29 ███▌░░░░░░░░  1  22 ██▌░░░░░░░░░  │
│ resets 14:30 · in 4.6h                                   ││                                          │
│ trend needs 6m of data (0s so far)                       ││ MEM █████████████░░░░░ 10.8G/14.6G       │
│                                                          ││ ZRAM ███▌░░░░░░░░░░░░ 2.6G→793M 3.4×     │
│ Weekly · all models                            4% used   ││ DISK ░░░░░░░░░░░░░░░░ 0B swapped         │
│ █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░                           ││                                          │
│ resets Fri 18:30 · in 2.4d                               ││ NET ↓41.8K/s      ↑2.7M/s                │
│ flat                                                     ││           ▁   ▂▃▃▅█                  █▅  │
│                                                          ││                                          │
│                                                          ││ PWR ⚡96%    9.7W    16m to full         │
│ LIMIT HITS (7d)  1                                       ││ █████████████████████░ health 97%        │
│ last Thu 00:54 · 6.4d ago                                ││ LCD 50%  █████░░░░░                      │
│ per-model limits are not exposed to local tools          ││     on AC — measuring pauses             │
│                                                          ││                                          │
│ opus-5 100%  <synthetic> 0%                              ││ /          ████▌░░░░░░░ 281G free        │
╰───────────────────────────────────────────────────── j/k ╯╰──────────────────────────────────────────╯
╭─ Docker ─────────────────────────────────────────────────╮╭─ Coherence ──────────────────────────────╮
│ ▼ career-ops                                             ││ ▲ COHERENT                               │
│ ▸ ● career-ops-kit-publisher                   Up 2 days ││ tunnelled via :12334                     │
│   ○ career-ops-web                             Exited (1 ││                                          │
│                                                          ││ ● Throne     listening :12334            │
│                                                          ││ ○ GNOME      none                        │
│ ──────────────────────────────────────────────────────── ││ ● shell env  http://127.0.0.1:12334      │
│ career-ops-kit-publisher      service kit-p3ce8bf28ca80  ││ ○ apt        direct + per-host           │
│ image      mcr.microsoft.com/playwright:v1.61.1-jammy    ││ ● zed        http://127.0.0.1:12334      │
│ state      running · Up 2 days                           ││ ● ssh        CONNECT via proxy           │
│ run        started 23 Aug 12:16 · unless-stopped         ││ ● DNS        Throne resolver :5533       │
│ cpu / mem  0.0%   5.6M / 14.6G  (0%)                     ││                                          │
│ net        ↓30.1K  ↑126B                                 ││ exit         2a01:4f8:c010:339a::1       │
│ disk       read 31.8M  write 1.9M                        ││                                          │
│ pids       3                                             ││ TIME                                     │
│ ports      none published                                ││ Tampere   09:23  Wed 26 Aug  −30m        │
│ network    career-ops_default  172.18.0.2                ││ Tehran    09:53  Wed 26 Aug  local       │
│ mounts     ~/CodeBase/AmirSalmani → /work [bind,rw]      ││ gap 1h30 from 25 Oct                     │
│ command    bash -lc 'echo '[kit-publisher] watching /wor ││                                          │
╰────────────────────────────────────── j/k  L logs  S/s/R ╯╰─────────────────────────────── i exit-IP ╯
 ? keys   Tab panel   r refresh   q quit                                                      50MB · 1s
```

## Why

Turning the VPN off does not turn the proxy off.

The proxy lives in half a dozen places that share no state — GNOME, the shell
environment, apt, Zed, ssh — and they drift apart silently. You switch Throne
off, GNOME goes quiet, and every process you started an hour ago is still
exporting `http_proxy=127.0.0.1:12334` pointed at a socket that no longer
exists. Nothing tells you. The failure is not an error, it is a hang.

`taq` watches all of it continuously and gives one verdict: **COHERENT** or
**SPLIT**. When it is SPLIT it names the running processes still holding the
dead proxy — which nothing else does, because a process's environment is frozen
at exec time and cannot be inspected from a config file.

The Claude panel exists for the adjacent question: not "how much have I used"
but "will I run out before the window resets".

## Install

```sh
git clone git@github.com:amir-salmani/taq.git
cd taq
ln -s "$PWD/bin/taq" ~/.local/bin/taq     # or: pip install -e .
taq
```

Rate-limit percentages reach a local tool through exactly one channel: the JSON
payload Claude Code pipes to a `statusLine` command. They are not in the
transcripts. So the quota panel needs a hook:

```sh
taq install       # backs up settings.json; refuses to overwrite an existing statusLine
```

This adds a status line to Claude Code (which replaces most of the built-in
footer hints — that is Claude Code's behaviour, not taq's). Undo by deleting the
`statusLine` key from `~/.claude/settings.json`.

## Keeping the numbers fresh

Only Claude Code's **terminal UI** draws a status line. Sessions hosted by the
SDK — Zed's agent panel, anything driving Claude Code programmatically — have no
status bar, so they never invoke the hook. Work exclusively in an editor and the
percentages freeze: they sat four days stale here, reading 15% while the truth
was 47%.

`taq-refresh` opens a throwaway terminal session, lets it render once, and stops
it. Run it by hand, or on a timer:

```sh
taq-refresh --force
systemctl --user enable --now taq-refresh.timer   # every 10 minutes
```

It costs nothing against your quota. The 5-hour window starts when a message is
*sent*, and this never sends one — it opens the UI, which reports the limits it
already knows, and exits. About 350 MB for 12 seconds.

Two things it has to get right, both learned the hard way:

- **It must start in a trusted directory.** Claude Code asks "do you trust this
  folder?" anywhere it has not seen before, and that dialog blocks before the UI
  renders, so the refresh silently times out. It reads `~/.claude.json` for a
  directory already marked trusted and runs there.
- **It must not `pkill claude`.** That would take your real sessions with it.
  The child goes into its own process group via `setsid`, and only that group is
  killed.

The panel judges freshness by the age of the reading, not by which sessions are
open — otherwise a two-minute-old number would be labelled frozen the moment the
refresher exits.

## Second clock in the GNOME top bar

An optional companion, not part of the TUI: `extras/gnome-shell/` holds a GNOME
Shell extension that puts a second timezone next to the panel clock.

```
Aug 26  09:52 | 09:22
```

GNOME can show world clocks inside the calendar popover, but nothing in the
panel itself — and the popover is a place you have to decide to open. When the
people you work with are in one country and the people you call are in another,
that number belongs where you glance without deciding to.

```sh
extras/gnome-shell/install.sh     # then log out and back in
```

The zone defaults to `Europe/Helsinki`; put a different tz name in
`~/.config/taq/second-zone` to change it. It follows your 12h/24h setting, and
runs no timer of its own — the panel clock already ticks once a minute, so the
extension follows that label instead of scheduling anything.

Two constraints worth knowing, both hit while building it: a **symlink into the
extensions directory does not work** (the shell's scanner does not follow them,
so `install.sh` copies), and **the shell only scans at startup** — on Wayland
there is no way to load a new extension without logging out.

## Commands

| | |
|---|---|
| `taq` | the TUI |
| `taq doctor` | everything taq can see, as plain text |
| `taq line` | one-line verdict for tmux / a prompt / waybar |
| `taq install` | register the statusline hook |
| `taq statusline` | internal; reads a payload on stdin |
| `taq-refresh` | open a throwaway session so the limits update (see below) |

`taq line` exits non-zero when the verdict is not COHERENT, so it works in a
conditional:

```sh
taq line   # ▲ 62% ◆3/4        …or…   ▼ 62% SPLIT(21) ◆3/4
```

## Keys

| | |
|---|---|
| `Tab` / `1`-`4` | move focus between panels |
| `j` `k` `↑` `↓` | move selection in the focused panel |
| `g` `G` | top / bottom |
| `L` / `Enter` | container logs |
| `S` `s` `R` `p` | start / stop / restart / pause a container (each confirms) |
| `r` | force a refresh |
| `i` | re-check the exit IP |
| `?` | keys |
| `q` `Esc` | close overlay, or quit |

There is deliberately no `rm` and no `prune`. This is a HUD, not a way to lose
a container.

## How it stays cheap

Measured at ~1.3% of one core and 26 MB resident with the tick at its fastest.
The tick backs off to 3 s and then 15 s when nothing is changing, and every
source is gated behind its own cadence, so an idle `taq` costs roughly a tenth
of that.

The method is just picking the cheap interface every time:

- **`/proc/net/tcp`** instead of spawning `ss` to find listening ports.
- **The Docker socket over `http.client`** instead of `docker ps`. The CLI costs
  ~50 ms and ~30 MB per invocation to dial the same socket.
- **`stats?one-shot=true`** with the CPU delta computed here. Plain
  `stream=false` blocks ~1 s *per container* while the daemon collects two
  samples — three containers stalled the whole UI loop for 5 s.
- **Byte-offset tailing** of the Claude transcripts. A `b'"usage"' in line`
  prefilter skips ~95% of a 270 MB tree before `json` sees it, so the cold scan
  is ~1 s and every later read is only the appended bytes. No index file, and
  therefore no index staleness.
- **Line-at-a-time parsing**, because reading a 50 MB transcript whole and
  splitting it costs ~100 MB of peak RSS — the entire budget.
- **`malloc_trim` after the cold scan.** Parsing a ~470 MB tree leaves ~23 MB of
  freed-but-retained arenas behind: glibc keeps them for reuse, and since the
  steady-state index is about 2 MB they are never reused. That was nearly half
  the resident footprint — 48 MB against 26. `gc.collect()` alone does nothing,
  because the objects are already gone; it is the allocator holding the pages.

The numbers are measured, not estimated, and they are checked against the kernel
rather than trusted: taq's own footer agreed with `VmRSS` to 1 MB when tested.

## What the panels show

**Claude** — plan tier, and the two limits using the same words as the web usage
page ("Current session", "Weekly · all models") so the screens can be compared
without translating. Plus a burn-rate projection, live sessions with their
memory use, and output tokens per project.

It also counts the times you actually hit a limit, read straight out of the
transcripts:

```
LIMIT HITS (7d)  4
last Tue 15:51 · 30h ago
```

That number comes from a different source than the bars — every session writes
a transcript whatever launched it — so it stays true even when the percentages
cannot be refreshed.

**Coherence** — every layer that could be routing your traffic, one verdict over
them, and the running processes still holding a dead proxy. This is the panel
the whole project exists for; see [Why](#why).

It also carries two clocks, because the machine sits between two places:

```
TIME
Tampere   09:19  Wed 26 Aug  −30m
Tehran    09:49  Wed 26 Aug  local
gap 1h30 from 25 Oct
```

The times are the easy half. The gap is the half that catches people: Finland
observes DST, Iran abolished it in 2022, so the difference is 30 minutes for
part of the year and 90 for the rest, and it flips on a date neither country
announces to you. taq finds the next flip by walking the offset forward rather
than hardcoding it. "local" is whichever zone matches the system clock, so the
labels swap themselves after a move. Zones live in `taq/clocks.py`.

**Docker** — containers grouped by compose project, with health and live
CPU/memory, and a detail pane for whichever row the cursor is on: image, state,
uptime, restart count and policy, CPU/memory/network/disk IO, pids, published
ports, networks and IPs, mounts, and the command. Almost all of it comes from
the list response already being fetched; only restart count, start time and
policy need an inspect, and only for that one row.

**System** — CPU with a braille history plot and per-core meters, memory,
network, disks, temperature — and power: charge, draw in watts, and **time left
computed from the actual current draw** rather than a vendor guess, plus battery
health against design capacity and cycle count.

Swap is split, because one combined number lies on a zram system:

```
MEM  ████████████░░░░ 11.1G/14.6G
ZRAM ███████████░░░   6.3G→1.9G 3.4×
DISK ██▌░░░░░░░░░░░   732M swapped
```

zram is compressed pages held in RAM — reading one back costs microseconds.
Disk swap is a disk trip. Reporting them together as "7.6G swapped" makes a
perfectly healthy machine look like it is drowning, which is how you end up
shopping for RAM you do not need. taq also surfaces the number that actually
answers the question — PSI memory stall time — but only when it is non-zero,
because a full-looking machine with no stalls is not short of anything.

**Backlight** — brightness, and what it costs you. taq records the machine's
power draw at each brightness level (only while on battery, and only when the
CPU is quiet, since a busy core swings power by 20W and would drown a 2W
backlight difference), then tells you what another level would buy:

```
LCD 60%  █████████████████▌░░░░░░░░░░░
    at 30%: 7.1W  +1h20m
```

That number is measured on your hardware, not modelled. It needs a few idle
samples at more than one brightness level before it will say anything, and it
persists across runs in `~/.local/state/taq/power.json`.

## Limits worth knowing

- **The web usage page shows a per-model weekly bar (Fable) that taq cannot.**
  The statusline payload carries only `five_hour` and `seven_day`; there is no
  per-model breakdown in it. The panel says so rather than quietly omitting it.
  The plan tier comes from `~/.claude.json`, which is ordinary config — taq
  never opens `~/.claude/.credentials.json`, and makes no API calls.
- The statusline hook only activates for sessions started **after** it is
  installed. `settings.json` is read at session start.
- The stale-process scan reads `/proc/<pid>/environ`, which is readable only for
  your own uid. System services are invisible to it, and it does not pretend
  otherwise.
- Burn-rate projection regresses on the reported percentage over time, never on
  token counts — mapping tokens to rate-limit consumption would mean guessing
  Anthropic's weighting. The bar for a credible trend scales with the window:
  2% of its length, so 6 minutes for the 5-hour budget and 3.4 hours for the
  weekly one. Below that it says what it is waiting for instead of guessing —
  an earlier version extrapolated a weekly cap from twenty minutes of data and
  confidently announced a breach 3.5 days early.
- Container CPU% is measured over taq's own poll interval, so the first reading
  after startup is 0.
- Layer detection is tuned for this setup: Throne, GNOME, apt, Zed, ssh. Paths
  live in `taq/paths.py`.

## Licence

MIT
