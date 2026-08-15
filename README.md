# taq

A terminal HUD for the four things that are true about your machine right now:
how much Claude quota is left, whether your traffic is actually going where you
think it is, what Docker is doing, and what the system is doing.

Stdlib Python, no dependencies, ~28 MB resident.

```
 taq   1 Claude   2 Docker   3 System   4 Coherence            ◆ 3/4   ▲ COHERENT   22:12
╭─ Claude ─────────────────────────────────────────╮╭─ System ────────────────────────╮
│ 5h ████████████████░░░░░░░░░░  62.4%             ││ CPU   8.9%  57°C   load 2.51    │
│    resets 17:10 · in 1.4h                        ││ ⠀⠀⠀⠀⠀⠀⠀⣀⣤⣆⣀⣠⣤⣶⣤⣄⣀⣤⣶⣿⣷⣤⣀⣠⣴⣶⣿⣷⣦⣄⡀ │
│    ⚠ cap ~16:31 — 39m early                      ││  0 ░░░░░  6   1 ███▌░ 75        │
│                                                  ││ MEM █████████████░░░ 10.3G/14.6G│
│ SESSIONS (7)                                     ││ NET ↓730K/s      ↑1.3M/s        │
│ ▸ ● amirsalmani-0e    busy                       ││ up 3.3d  2732 procs  🔋94%      │
╰──────────────────────────────────────────── j/k ─╯╰─────────────────────────────────╯
╭─ Docker ─────────────────────────────────────────╮╭─ Coherence ─────────────────────╮
│ ▼ myproject                                      ││ ▲ COHERENT                      │
│   ● api          100.9%   424K Up 3 minutes      ││ tunnelled via :12334            │
│ ▸ ● web            2.0%  10.9M Up 3m (healthy)   ││ ● Throne     listening :12334   │
│   ○ migrate                    Exited (0)        ││ ● shell env  127.0.0.1:12334    │
╰──────────────────────── j/k  L logs  S/s/R ──────╯╰──────────────────────────────────╯
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

## Limits worth knowing

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
