# taq

A terminal HUD for the four things that are true about your machine right now:
how much Claude quota is left, whether your traffic is actually going where you
think it is, what Docker is doing, and what the system is doing.

Stdlib Python, no dependencies, ~28 MB resident.

```
 taq   1 Claude   2 Docker   3 System   4 Coherence           ◆ 2/3   ▲ COHERENT   23:57
╭─ Claude ─────────────────────────────────────────╮╭─ System ─────────────────────────╮
│ Plan usage limits Max (5x)                       ││ CPU   8.4%  53°C   load 1.02     │
│                                                  ││ ⠀⠀⣀⣤⣆⣀⣠⣤⣶⣤⣄⣀⣤⣶⣿⣷⣤⣀⣠⣴⣶⣿⣷⣦⣄⡀⣠⣴⣶⣿⣷⣦⣄ │
│ Current session                        62% used  ││  0  10 ▌░░░░  1   8 ░░░░░        │
│ ██████████████████░░░░░░░░░░░                    ││  2   9 ▌░░░░  3  64 ███░░        │
│ resets 03:00 · in 3h                             ││ MEM ████████████░░░ 10.8G/14.6G  │
│ ⚠ hits the cap ~02:21, 39m early                 ││ SWP ████████▌░░░░░░ 7.4G         │
│                                                  ││ NET ↓3.9K/s      ↑1.2K/s         │
│ Weekly · all models                    14% used  ││                                  │
│ ████░░░░░░░░░░░░░░░░░░░░░░░░░                    ││ PWR 🔋58%   9.1W   4.1h left     │
│ resets Fri 18:30 · in 5.8d                       ││ ███████████░░░░░░░░  health 96%  │
│ +2.4%/h — clears the window                      ││ LCD 60%  ██████████▌░░░░░░       │
│                                                  ││     at 30%: 7.1W  +1h20m         │
│ per-model limits are not exposed to local tools  ││ /       ███████░░░░░░ 329G free  │
│                                                  ││ up 3.4d  2732 procs  14 cycles   │
│ SESSIONS (7)                                     │╰──────────────────────────────────╯
│ ▸ ● amirsalmani-0e    busy                       │╭─ Coherence ──────────────────────╮
│   ○ vuhom-09          idle 2m                    ││ ▲ COHERENT                       │
│                                                  ││ tunnelled via :12334             │
│ OUTPUT TOKENS (7d)                               ││ ● Throne     listening :12334    │
│ Lotusion         2.3M ████████████████████       ││ ● GNOME      manual              │
╰──────────────────────────────────────────── j/k ─╯│ ● shell env  127.0.0.1:12334     │
╭─ Docker ─────────────────────────────────────────╮│ ○ apt        direct + per-host   │
│ ▼ career-ops                                     ││ ● zed        127.0.0.1:12334     │
│ ▸ ● web            2.0%  10.9M Up 3m (healthy)   ││ ● DNS        Throne resolver     │
│   ○ migrate                    Exited (143)      ││                                  │
│ ───────────────────────────────────────────────  ││ exit         194.5.207.166       │
│ web                 service web    aa0d354c1f9d  ││                                  │
│ image      node:24-alpine                        ││ STALE ENVIRONMENT (21)           │
│ state      running · Up 3 minutes (healthy)      ││ started while the proxy was up,  │
│ run        started 15 Aug 22:50 · unless-stopped ││ still pointing at it — restart.  │
│ cpu / mem  2.0%   10.9M / 14.6G  (1%)            ││ claude       ×7  1115452 1458097 │
│ net        ↓108M  ↑6.7M                          ││ bash         ×6  6769 7018 …     │
│ ports      127.0.0.1:3000→3000/tcp               ││ zed-editor   ×2  1114638 1114676 │
│ mounts     ~/CodeBase/app → /app [bind,rw]       ││                                  │
╰──────────────────────── j/k  L logs  S/s/R ──────╯╰──────────────────────────────────╯
 ? keys   Tab panel   r refresh   q quit                                    28MB · 1s
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

## Commands

| | |
|---|---|
| `taq` | the TUI |
| `taq doctor` | everything taq can see, as plain text |
| `taq line` | one-line verdict for tmux / a prompt / waybar |
| `taq install` | register the statusline hook |
| `taq statusline` | internal; reads a payload on stdin |

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

Measured at ~1.0% of one core and 27.8 MB resident with four containers running
and the tick at its fastest. The tick backs off to 3 s and then 15 s when
nothing is changing, and every source is gated behind its own cadence, so an
idle `taq` costs roughly a tenth of that.

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

## What the panels show

**Claude** — plan tier, and the two limits using the same words as the web usage
page ("Current session", "Weekly · all models") so the screens can be compared
without translating. Plus a burn-rate projection, live sessions, and output
tokens per project.

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
  Anthropic's weighting. It needs 3 samples over 5 minutes before it will
  commit to an ETA.
- Container CPU% is measured over taq's own poll interval, so the first reading
  after startup is 0.
- Layer detection is tuned for this setup: Throne, GNOME, apt, Zed, ssh. Paths
  live in `taq/paths.py`.

## Licence

MIT
