# bupt-ucloud-tool

Async CLI for downloading course resources from BUPT uCloud.

## Install

```sh
uv tool install git+https://github.com/OneZ3r0/bupt-ucloud-tool.git
```

Or run from source:

```sh
git clone https://github.com/OneZ3r0/bupt-ucloud-tool.git
cd bupt-ucloud-tool
uv sync
```

## Usage

```sh
bupt-ucloud            # interactive mode (installed via uv tool)
bupt-ucloud logout     # clear saved session

uv run main.py         # if running from source
```

On first run, you'll be prompted for CAS credentials. Sessions are cached at `~/.config/bupt-ucloud-tool/session.json`.