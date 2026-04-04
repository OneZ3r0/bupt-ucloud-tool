"""BUPT uCloud CLI — async course resource downloader."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx
import questionary
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TextColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

# Constants
CAS_LOGIN_URL = (
    "https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn"
)
API_BASE = "https://apiucloud.bupt.edu.cn"
API_TOKEN_URL = f"{API_BASE}/ykt-basics/oauth/token"

YKT_HEADERS = {
    "Authorization": "Basic  cG9ydGFsOnBvcnRhbF9zZWNyZXQ=",
    "Tenant-Id": "000000",
    "Identity": "JS005:undefined",
}

EXECUTION_RE = re.compile(r'<input name="execution" value="(.*?)"')

console = Console()

_BACK = "__back__"
_EXIT = "__exit__"
_NAV_HINT = "(enter=confirm, b=back, q=quit)"


def _add_nav_keys(question: questionary.Question) -> questionary.Question:
    """Inject back/exit key bindings into a questionary prompt."""
    kb = question.application.key_bindings

    @kb.add("b", eager=True)
    def _back(event: object) -> None:
        event.app.exit(result=_BACK)  # type: ignore[attr-defined]

    @kb.add("q", eager=True)
    def _exit(event: object) -> None:
        event.app.exit(result=_EXIT)

    return question


# Models
class Session(BaseModel):
    """Persisted session data."""

    access_token: str
    user_id: str
    expires_at: float


class Course(BaseModel):
    """Course info from the API."""

    id: str
    site_name: str = Field(alias="siteName")

    model_config = {"populate_by_name": True}


class Attachment(BaseModel):
    """Downloadable attachment."""

    name: str
    url: str
    size: str = ""


# Client
class BUPTClient:
    """BUPT uCloud client — handles auth, API calls, and downloads."""

    SESSION_DIR = Path.home() / ".config" / "bupt-ucloud-tool"
    SESSION_FILE = SESSION_DIR / "session.json"
    DOWNLOAD_DIR = Path.cwd() / "downloads"

    def __init__(self) -> None:
        self._session: Session | None = None
        self._client: httpx.AsyncClient | None = None

    # Lifecycle
    async def __aenter__(self) -> BUPTClient:
        self._client = httpx.AsyncClient(
            headers={**YKT_HEADERS},
            timeout=30.0,
            follow_redirects=False,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        assert self._client is not None, (
            "Use `async with BUPTClient()` as context manager"
        )
        return self._client

    # Session persistence
    def _load_session(self) -> Session | None:
        """Load cached session; returns None if missing or expired."""
        if not self.SESSION_FILE.exists():
            return None
        try:
            data = json.loads(self.SESSION_FILE.read_text())
            session = Session.model_validate(data)
            if time.time() >= session.expires_at:
                console.print("[yellow]⚠ Session expired, please re-login[/]")
                return None
            return session
        except (json.JSONDecodeError, ValidationError, OSError):
            return None

    def _save_session(self, session: Session) -> None:
        """Persist session with restricted permissions."""
        self.SESSION_DIR.mkdir(parents=True, exist_ok=True)
        self.SESSION_FILE.write_text(session.model_dump_json(indent=2))
        os.chmod(self.SESSION_FILE, 0o600)

    @staticmethod
    def _decode_jwt_exp(token: str) -> float:
        """Extract `exp` from JWT payload without signature verification."""
        try:
            payload_b64 = token.split(".")[1] + "=="
            payload = json.loads(base64.b64decode(payload_b64))
            return float(payload.get("exp", 0))
        except Exception:
            return 0.0

    def logout(self) -> None:
        """Remove local session file."""
        if self.SESSION_FILE.exists():
            self.SESSION_FILE.unlink()
            console.print("[green]✓ Logged out[/]")
        else:
            console.print("[dim]Not logged in[/]")

    # Authentication
    async def login(self) -> None:
        """Restore cached session or perform CAS → ticket → JWT login."""
        cached = self._load_session()
        if cached:
            self._session = cached
            self.client.headers["Blade-Auth"] = cached.access_token
            console.print(
                f"[green]✓ Restored session from cache (user_id: {cached.user_id})[/]"
            )
            return

        console.print("[bold]🔐 BUPT CAS Login[/]")

        res = await self.client.get(CAS_LOGIN_URL)
        res.raise_for_status()

        match = EXECUTION_RE.search(res.text)
        if not match:
            console.print(
                "[red]✗ Failed to extract execution param, CAS page may have changed[/]"
            )
            sys.exit(1)

        execution = match.group(1)

        username = await questionary.text("Username:").ask_async()
        password = await questionary.password("Password:").ask_async()
        if not username or not password:
            console.print("[red]✗ Username and password cannot be empty[/]")
            sys.exit(1)

        login_data = {
            "username": username,
            "password": password,
            "submit": "LOGIN",
            "type": "username_password",
            "execution": execution,
            "_eventId": "submit",
        }

        res2 = await self.client.post(
            CAS_LOGIN_URL,
            data=login_data,
            cookies=res.cookies,
        )

        location = res2.headers.get("Location", "")
        if "ticket=" not in location:
            console.print("[red]✗ Login failed: invalid username or password[/]")
            sys.exit(1)

        ticket = location.split("ticket=")[-1]

        res3 = await self.client.post(
            API_TOKEN_URL,
            data={"ticket": ticket, "grant_type": "third"},
        )
        res3.raise_for_status()

        token_info = res3.json()
        access_token = token_info.get("access_token")
        user_id = token_info.get("user_id")
        if not access_token or not user_id:
            console.print(f"[red]✗ Failed to obtain token: {token_info}[/]")
            sys.exit(1)

        session = Session(
            access_token=access_token,
            user_id=str(user_id),
            expires_at=self._decode_jwt_exp(access_token),
        )
        self._session = session
        self._save_session(session)
        self.client.headers["Blade-Auth"] = access_token

        console.print(f"[green]✓ Login successful (user_id: {user_id})[/]")

    # API calls
    @property
    def session(self) -> Session:
        assert self._session is not None, "Call login() first"
        return self._session

    async def get_courses(self) -> list[Course]:
        """Fetch the current semester's course list."""
        res = await self.client.get(
            f"{API_BASE}/ykt-site/site/list/student/current",
            params={
                "size": 999999,
                "current": 1,
                "userId": self.session.user_id,
                "siteRoleCode": 2,
            },
        )
        res.raise_for_status()

        data = res.json()
        records = data.get("data", {}).get("records")
        if not isinstance(records, list):
            console.print(f"[red]✗ Unexpected course list format: {data}[/]")
            return []

        courses: list[Course] = []
        for r in records:
            try:
                courses.append(Course.model_validate(r))
            except ValidationError:
                continue
        return courses

    async def get_resources(self, site_id: str) -> list[Attachment]:
        """Fetch downloadable attachments for a course."""
        res = await self.client.post(
            f"{API_BASE}/ykt-site/site-resource/tree/student",
            params={"userId": self.session.user_id, "siteId": site_id},
        )
        res.raise_for_status()

        data = res.json()
        resources = data.get("data")
        if not isinstance(resources, list):
            console.print(f"[red]✗ Unexpected resource list format: {data}[/]")
            return []

        attachments: list[Attachment] = []
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            resource_name = resource.get("resourceName", "—")
            for vo in resource.get("attachmentVOs") or []:
                inner = vo.get("resource") if isinstance(vo, dict) else None
                if not isinstance(inner, dict):
                    continue
                name = inner.get("name", "unknown")
                url = inner.get("url", "")
                size = inner.get("fileSizeUnit", "")
                if url:
                    attachments.append(
                        Attachment(name=f"[{resource_name}] {name}", url=url, size=size)
                    )
        return attachments

    # Download
    async def _download_one(
        self,
        dl_client: httpx.AsyncClient,
        att: Attachment,
        dest_dir: Path,
        progress: Progress,
        task_id: TaskID,
    ) -> None:
        """Stream-download a single file."""
        dest = dest_dir / att.name.split("] ")[-1]
        async with dl_client.stream("GET", att.url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            progress.update(task_id, total=total or None)
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(8192):
                    f.write(chunk)
                    progress.advance(task_id, len(chunk))

    async def download_files(
        self, files: list[Attachment], dest_dir: Path | None = None
    ) -> None:
        """Download files concurrently with a rich progress bar."""
        dest = dest_dir or self.DOWNLOAD_DIR
        dest.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as dl_client:
            with Progress(
                TextColumn("[bold blue]{task.fields[filename]}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                console=console,
            ) as progress:
                tasks: list[tuple[Attachment, TaskID]] = []
                for att in files:
                    tid = progress.add_task("download", filename=att.name, total=None)
                    tasks.append((att, tid))

                await asyncio.gather(
                    *(
                        self._download_one(dl_client, att, dest, progress, tid)
                        for att, tid in tasks
                    )
                )

        console.print(f"\n[green]✓ Downloaded {len(files)} file(s) to {dest}[/]")


# CLI Entry
async def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "logout":
        BUPTClient().logout()
        return

    async with BUPTClient() as client:
        try:
            await client.login()

            courses = await client.get_courses()
            if not courses:
                console.print("[yellow]No courses found[/]")
                return

            while True:
                table = Table(title="📚 Courses", show_lines=True)
                table.add_column("#", style="dim", width=4)
                table.add_column("Course Name", style="cyan")
                table.add_column("ID", style="dim")
                for i, c in enumerate(courses, 1):
                    table.add_row(str(i), c.site_name, c.id)
                console.print(table)

                course_choices = [
                    questionary.Choice(title=c.site_name, value=c) for c in courses
                ]
                selected = await _add_nav_keys(
                    questionary.select(
                        "Select a course:",
                        choices=course_choices,
                        instruction=_NAV_HINT,
                    )
                ).ask_async()
                if not selected or selected in (_BACK, _EXIT):
                    return

                console.print(
                    f"\n[bold]📂 Fetching resources for [{selected.site_name}]...[/]"
                )
                attachments = await client.get_resources(selected.id)
                if not attachments:
                    console.print(
                        "[yellow]No downloadable attachments for this course[/]\n"
                    )
                    no_res = await _add_nav_keys(
                        questionary.select(
                            "No resources available:",
                            choices=[
                                questionary.Choice(
                                    title="Back to course list", value="back"
                                ),
                            ],
                            instruction=_NAV_HINT,
                        )
                    ).ask_async()
                    if no_res == _EXIT:
                        return
                    console.print()
                    continue

                while True:
                    att_table = Table(title="📎 Attachments", show_lines=True)
                    att_table.add_column("#", style="dim", width=4)
                    att_table.add_column("Filename", style="cyan")
                    att_table.add_column("Size", style="green", justify="right")
                    att_table.add_column("URL", style="dim", overflow="fold")
                    for i, a in enumerate(attachments, 1):
                        # Apply OSC 8 hyperlink to ensure it is fully clickable in terminals
                        url_text = Text(a.url)
                        url_text.stylize(f"link {a.url}")
                        att_table.add_row(str(i), a.name, a.size, url_text)
                    console.print(att_table)
                    console.print()

                    action = await _add_nav_keys(
                        questionary.select(
                            "What would you like to do?",
                            choices=[
                                questionary.Choice(
                                    title="Download files", value="download"
                                ),
                            ],
                            instruction=_NAV_HINT,
                        )
                    ).ask_async()

                    if action in (_BACK, None):
                        console.print()
                        break
                    if action == _EXIT:
                        return

                    download_choices = [
                        questionary.Choice(title=f"{a.name} ({a.size})", value=a)
                        for a in attachments
                    ]
                    selected_files = await _add_nav_keys(
                        questionary.checkbox(
                            "Select files to download:",
                            choices=download_choices,
                            instruction="(space=toggle, a=all, i=invert, enter=confirm, none=back, b=back, q=quit)",
                        )
                    ).ask_async()

                    if selected_files in (_BACK, _EXIT):
                        if selected_files == _EXIT:
                            return
                        continue
                    if not selected_files:
                        console.print(
                            "[dim]No files selected, back to attachments[/]\n"
                        )
                        continue

                    default_dir = str(BUPTClient.DOWNLOAD_DIR)
                    dest_input: str | None = await questionary.text(
                        "Download directory:",
                        default=default_dir,
                    ).ask_async()
                    if not dest_input:
                        continue
                    dest_dir = Path(dest_input).expanduser().resolve()

                    await client.download_files(selected_files, dest_dir=dest_dir)
                    console.print()

        except httpx.HTTPStatusError as e:
            console.print(
                f"[red]✗ HTTP error: {e.response.status_code} {e.request.url}[/]"
            )
            sys.exit(1)
        except httpx.ConnectError:
            console.print("[red]✗ Connection failed, check your network[/]")
            sys.exit(1)
        except httpx.TimeoutException:
            console.print("[red]✗ Request timed out[/]")
            sys.exit(1)
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled[/]")


def cli() -> None:
    """Synchronous entry point for the CLI (used by project.scripts)."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
