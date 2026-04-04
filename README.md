# bupt-ucloud-tool

Async CLI for downloading course resources from BUPT uCloud.

## Setup

```sh
uv sync
```

## Usage

```sh
uv run main.py          # interactive mode
uv run main.py logout   # clear saved session
```

On first run, you'll be prompted for CAS credentials. Sessions are cached at `~/.config/bupt-ucloud-tool/session.json`.