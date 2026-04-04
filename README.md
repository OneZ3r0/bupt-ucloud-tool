# bupt-ucloud-tool

Async CLI for downloading course resources from BUPT uCloud.

## Usage

Install or Update

```sh
uv tool install git+https://github.com/OneZ3r0/bupt-ucloud-tool.git

bupt-ucloud            # interactive mode (installed via uv tool)
bupt-ucloud logout     # clear saved session
```

Or run from source:

```sh
git clone https://github.com/OneZ3r0/bupt-ucloud-tool.git
cd bupt-ucloud-tool
uv sync

uv run main.py
```


On first run, you'll be prompted for CAS credentials. Sessions are cached at `~/.config/bupt-ucloud-tool/session.json`.