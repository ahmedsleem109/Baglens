# Quickstart — Windows 11 + WSL2

The shipped artifact runs on bare Linux, macOS and Windows. This page covers the
Windows + WSL2 path because it is the one with sharp edges.

## 1. WSL

```powershell
# PowerShell as Administrator
wsl --install -d Ubuntu-24.04
wsl --set-default-version 2
wsl --update
```

Indexing is memory-hungry. Put this in `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
memory=8GB
processors=4
swap=8GB
```

## 2. Inside WSL

```bash
sudo apt update && sudo apt install -y build-essential git curl pkg-config \
    libgl1 libglib2.0-0            # Pillow runtime deps
curl -LsSf https://astral.sh/uv/install.sh | sh
exec $SHELL

git clone https://github.com/yourname/baglens ~/dev/baglens
cd ~/dev/baglens && uv sync
uv run pytest -q
```

Try it against a generated recording:

```bash
uv run python -m tests.synth.generate --out ~/data
uv run python -c "
from baglens.detectors import Auditor
from baglens.readers import open_bag
r = Auditor(open_bag('$HOME/data/clean.mcap')).run()
print(r.verdict, r.overall_score)
"
```

## 3. Wire it to a client

```bash
uv run baglens --stdio --root ~/data
```

Then point your MCP client at it (see `examples/`). For a browser-based or HTTP client:

```bash
uv run baglens --http --port 8765
```

`localhost` forwarding works WSL2 → Windows, so `http://localhost:8765` from a Windows
client reaches the server.

## The gotchas, in the order they will bite you

1. **Keep data on the Linux filesystem.** `/mnt/c/...` goes through a translation layer
   and is roughly an order of magnitude slower for the many-small-reads pattern indexing
   produces. Put recordings in `~/data`, not `C:\data`. If they must live on Windows,
   expect the slowdown and say so in your own timings.

2. **Claude Desktop runs on Windows and cannot see WSL paths.** Bridge it:

   ```json
   {
     "mcpServers": {
       "baglens": {
         "command": "wsl.exe",
         "args": ["-d", "Ubuntu-24.04", "--cd", "/home/YOU/dev/baglens",
                  "--", "/home/YOU/.local/bin/uv", "run", "baglens", "--stdio"],
         "env": {}
       }
     }
   }
   ```

   Use the **absolute path** to `uv`. WSL non-login shells often do not have
   `~/.local/bin` on `PATH`, and the resulting failure is silent.

3. **Line endings.** `git config --global core.autocrlf input` before cloning, or every
   shell script breaks in confusing ways.

4. **Clock drift after sleep.** WSL2 clocks can skew after laptop suspend, which makes
   timestamp-sensitive tests flaky for reasons that have nothing to do with your code.
   `sudo hwclock -s` resets it.

5. **`inotify` is unreliable across `/mnt/c`.** Any directory-watching workflow is
   Linux-filesystem-only.

## Free test data

- Foxglove sample `.mcap` files — small, well-formed, ideal fixtures.
- The PX4 public flight-log review site — thousands of real flights *with real failures*,
  which is rare and valuable. `baglens` reads `.ulg` with the `ulog` extra.
- Open ROS 2 bag datasets on Hugging Face.
- **The synthetic generator in this repo** — the only source of *labelled* failures, and
  therefore the backbone of every number published here.

`scripts/fetch_public_data.sh` downloads and caches on demand. Keep local data under
~20 GB.
