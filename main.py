"""BUPT uCloud CLI — async assignment browser and resource downloader."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

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
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Constants
CAS_LOGIN_URL = (
    "https://auth.bupt.edu.cn/authserver/login?service=https://ucloud.bupt.edu.cn"
)
API_BASE = "https://apiucloud.bupt.edu.cn"
API_TOKEN_URL = f"{API_BASE}/ykt-basics/oauth/token"
FILE_BASE = "https://fileucloud.bupt.edu.cn/ucloud/document"

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
_INVALID_PATH_CHARS_RE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


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


def _safe_path_component(value: str, fallback: str = "unnamed") -> str:
    """Return a portable, non-traversing path component."""
    cleaned = _INVALID_PATH_CHARS_RE.sub("_", value).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    if cleaned.upper() in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:120].rstrip(" .") or fallback


def _safe_filename(value: str) -> str:
    """Sanitize a server-provided filename while preserving its suffix."""
    cleaned = _INVALID_PATH_CHARS_RE.sub("_", value).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        return "download"
    path = Path(cleaned)
    suffix = path.suffix[:20]
    max_stem_length = 120 - len(suffix)
    stem = path.stem[:max_stem_length].rstrip(" .") or "download"
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{suffix}"


def _unique_destination(
    dest_dir: Path, filename: str, reserved: set[Path] | None = None
) -> Path:
    """Choose a destination without overwriting disk or in-batch files."""
    if reserved is None:
        reserved = set()
    safe_name = _safe_filename(filename)
    candidate = dest_dir / safe_name
    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 1
    while candidate.exists() or candidate in reserved:
        candidate = dest_dir / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _build_download_dir(base: Path, path_components: tuple[str, ...]) -> Path:
    """Build a nested download directory from safe path components."""
    for component in path_components:
        base /= _safe_path_component(component)
    return base


class _AssignmentHTMLParser(HTMLParser):
    """Convert assignment HTML into readable terminal text."""

    _BLOCK_TAGS = {
        "blockquote",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "ol",
        "p",
        "pre",
        "table",
        "tr",
        "ul",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, int]] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag in self._BLOCK_TAGS or tag == "br":
            self.parts.append("\n")
        if tag == "a":
            self.links.append((attrs_dict.get("href") or "", len(self.parts)))
        elif tag == "img":
            src = attrs_dict.get("src")
            if src:
                self.parts.append(f"\n[Image: {src}]\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag == "a" and self.links:
            href, start = self.links.pop()
            label = "".join(self.parts[start:]).strip()
            if href and href not in label:
                self.parts.append(f" ({href})")
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    """Convert HTML to compact plain text, retaining links and image URLs."""
    parser = _AssignmentHTMLParser()
    parser.feed(value)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


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
    filename: str | None = None
    resource_id: str = ""
    file_type: str = ""

    @property
    def download_name(self) -> str:
        return self.filename or self.name


class AssignmentSummary(BaseModel):
    """Assignment entry returned by the course assignment list."""

    id: str
    assignment_title: str = Field(alias="assignmentTitle")
    assignment_end_time: str = Field(default="", alias="assignmentEndTime")

    model_config = {"populate_by_name": True}


class AssignmentResource(BaseModel):
    """Resource reference embedded in an assignment detail."""

    resource_id: str = Field(alias="resourceId")
    resource_name: str = Field(alias="resourceName")
    resource_type: str = Field(default="", alias="resourceType")

    model_config = {"populate_by_name": True}


class AssignmentDetail(BaseModel):
    """Full assignment content and its resource references."""

    id: str
    assignment_title: str = Field(alias="assignmentTitle")
    assignment_content: str = Field(default="", alias="assignmentContent")
    assignment_end_time: str = Field(default="", alias="assignmentEndTime")
    resources: list[AssignmentResource] = Field(
        default_factory=list, alias="assignmentResource"
    )

    model_config = {"populate_by_name": True}


class ResourceMetadata(BaseModel):
    """Storage metadata needed to construct an attachment download URL."""

    id: str
    name: str = ""
    file_size_unit: str = Field(default="", alias="fileSizeUnit")
    ext: str = ""
    storage_id: str = Field(alias="storageId")

    model_config = {"populate_by_name": True}


# Client
class BUPTClient:
    """BUPT uCloud client — handles auth, API calls, and downloads."""

    SESSION_DIR = Path.home() / ".config" / "bupt-ucloud-tool"
    SESSION_FILE = SESSION_DIR / "session.json"
    DOWNLOAD_DIR = Path.cwd() / "Downloads"

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
                        Attachment(
                            name=f"[{resource_name}] {name}",
                            filename=name,
                            url=url,
                            size=size,
                        )
                    )
        return attachments

    async def get_assignments(self, site_id: str) -> list[AssignmentSummary]:
        """Fetch all assignment pages for a course in server order."""
        assignments: list[AssignmentSummary] = []
        current = 1
        while True:
            res = await self.client.post(
                f"{API_BASE}/ykt-site/work/student/list",
                json={
                    "siteId": site_id,
                    "userId": self.session.user_id,
                    "keyword": "",
                    "current": current,
                    "size": 5,
                    "studentAssignmentStatus": None,
                    "status": 0,
                    "sortColumn": "",
                    "sortType": None,
                },
            )
            res.raise_for_status()

            data = res.json()
            page = data.get("data") if isinstance(data, dict) else None
            records = page.get("records") if isinstance(page, dict) else None
            if not isinstance(records, list):
                console.print(f"[red]✗ Unexpected assignment list format: {data}[/]")
                return assignments

            for record in records:
                try:
                    assignments.append(AssignmentSummary.model_validate(record))
                except ValidationError:
                    console.print("[yellow]⚠ Skipped an invalid assignment record[/]")

            pages = page.get("pages", current)
            if not isinstance(pages, int) or current >= pages:
                break
            current += 1

        return assignments

    async def get_assignment_detail(
        self, assignment_id: str
    ) -> AssignmentDetail | None:
        """Fetch a single assignment's content and resource references."""
        res = await self.client.get(
            f"{API_BASE}/ykt-site/work/detail",
            params={"assignmentId": assignment_id},
        )
        res.raise_for_status()

        data = res.json()
        detail = data.get("data") if isinstance(data, dict) else None
        if not isinstance(detail, dict):
            console.print(f"[red]✗ Unexpected assignment detail format: {data}[/]")
            return None
        valid_resources: list[dict[str, object]] = []
        raw_resources = detail.get("assignmentResource") or []
        if isinstance(raw_resources, list):
            for resource in raw_resources:
                try:
                    valid = AssignmentResource.model_validate(resource)
                except ValidationError:
                    console.print("[yellow]⚠ Skipped an invalid assignment resource[/]")
                    continue
                valid_resources.append(valid.model_dump(by_alias=True))
        detail = {**detail, "assignmentResource": valid_resources}
        try:
            return AssignmentDetail.model_validate(detail)
        except ValidationError:
            console.print("[red]✗ Assignment detail is missing required fields[/]")
            return None

    async def _get_resource_metadata(self, resource_id: str) -> ResourceMetadata | None:
        """Resolve one resource ID to its storage metadata."""
        try:
            res = await self.client.get(
                f"{API_BASE}/blade-source/resource/list/byId",
                params={"resourceIds": resource_id},
            )
            res.raise_for_status()
        except httpx.HTTPError:
            console.print(f"[yellow]⚠ Failed to resolve resource {resource_id}[/]")
            return None

        data = res.json()
        records = data.get("data") if isinstance(data, dict) else None
        if not isinstance(records, list):
            console.print(
                f"[yellow]⚠ Unexpected metadata for resource {resource_id}; skipped[/]"
            )
            return None
        for record in records:
            if not isinstance(record, dict) or str(record.get("id")) != resource_id:
                continue
            try:
                return ResourceMetadata.model_validate(record)
            except ValidationError:
                break
        console.print(f"[yellow]⚠ Resource {resource_id} could not be resolved[/]")
        return None

    async def get_assignment_attachments(
        self, resources: list[AssignmentResource]
    ) -> list[Attachment]:
        """Resolve assignment resources into downloadable attachments."""
        metadata = await asyncio.gather(
            *(
                self._get_resource_metadata(resource.resource_id)
                for resource in resources
            )
        )

        attachments: list[Attachment] = []
        for resource, resolved in zip(resources, metadata, strict=True):
            if resolved is None:
                continue
            extension = re.sub(
                r"[^A-Za-z0-9]",
                "",
                (resolved.ext or resource.resource_type).lstrip("."),
            )
            if not resolved.storage_id or not extension:
                console.print(
                    f"[yellow]⚠ Resource {resource.resource_id} has no storage ID "
                    "or type; skipped[/]"
                )
                continue

            filename = resource.resource_name or resolved.name or resource.resource_id
            if not Path(filename).suffix:
                filename = f"{filename}.{extension}"
            storage_id = quote(resolved.storage_id, safe="")
            encoded_ext = quote(extension, safe="")
            attachments.append(
                Attachment(
                    name=filename,
                    filename=filename,
                    url=f"{FILE_BASE}/{storage_id}.{encoded_ext}",
                    size=resolved.file_size_unit,
                    resource_id=resource.resource_id,
                    file_type=resource.resource_type or resolved.ext,
                )
            )
        return attachments

    # Download
    async def _download_one(
        self,
        dl_client: httpx.AsyncClient,
        att: Attachment,
        dest: Path,
        progress: Progress,
        task_id: TaskID,
    ) -> None:
        """Stream-download a single file."""
        temp = dest.with_name(f".{dest.name}.part")
        try:
            async with dl_client.stream("GET", att.url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length", 0))
                progress.update(task_id, total=total or None)
                with temp.open("wb") as file:
                    async for chunk in resp.aiter_bytes(8192):
                        file.write(chunk)
                        progress.advance(task_id, len(chunk))
            temp.replace(dest)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

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
                tasks: list[tuple[Attachment, Path, TaskID]] = []
                reserved: set[Path] = set()
                for att in files:
                    destination = _unique_destination(
                        dest, att.download_name, reserved=reserved
                    )
                    reserved.add(destination)
                    tid = progress.add_task(
                        "download", filename=destination.name, total=None
                    )
                    tasks.append((att, destination, tid))

                await asyncio.gather(
                    *(
                        self._download_one(dl_client, att, destination, progress, tid)
                        for att, destination, tid in tasks
                    )
                )

        console.print(f"\n[green]✓ Downloaded {len(files)} file(s) to {dest}[/]")


# CLI helpers
def _print_attachments(
    attachments: list[Attachment], *, title: str, show_resource_info: bool = False
) -> None:
    table = Table(title=title, show_lines=True)
    table.add_column("#", style="dim", width=4)
    table.add_column("Filename", style="cyan")
    if show_resource_info:
        table.add_column("Type", style="magenta")
        table.add_column("Resource ID", style="dim")
    table.add_column("Size", style="green", justify="right")
    table.add_column("URL", style="dim", overflow="fold")
    for index, attachment in enumerate(attachments, 1):
        url_text = Text(attachment.url)
        url_text.stylize(f"link {attachment.url}")
        row: list[str | Text] = [str(index), Text(attachment.name)]
        if show_resource_info:
            row.extend([Text(attachment.file_type), Text(attachment.resource_id)])
        row.extend([attachment.size, url_text])
        table.add_row(*row)
    console.print(table)
    console.print()


async def _prompt_download(
    client: BUPTClient,
    attachments: list[Attachment],
    path_components: tuple[str, ...] = (),
) -> bool:
    """Prompt for files and destination; return True when the user quits."""
    choices = [
        questionary.Choice(
            title=f"{attachment.name} ({attachment.size})", value=attachment
        )
        for attachment in attachments
    ]
    selected_files = await _add_nav_keys(
        questionary.checkbox(
            "Select files to download:",
            choices=choices,
            instruction=(
                "(space=toggle, a=all, i=invert, enter=confirm, "
                "none=back, b=back, q=quit)"
            ),
        )
    ).ask_async()
    if selected_files == _EXIT:
        return True
    if selected_files == _BACK:
        return False
    if not selected_files:
        console.print("[dim]No files selected, back to attachments[/]\n")
        return False

    dest_input: str | None = await questionary.text(
        "Download directory:", default=str(BUPTClient.DOWNLOAD_DIR)
    ).ask_async()
    if not dest_input:
        return False

    dest_dir = _build_download_dir(
        Path(dest_input).expanduser().resolve(), path_components
    )
    await client.download_files(selected_files, dest_dir=dest_dir)
    console.print()
    return False


async def browse_course_resources(client: BUPTClient, course: Course) -> bool:
    """Run the existing course-resource workflow; return True on quit."""
    console.print(Text(f"\n📂 Fetching resources for [{course.site_name}]...", style="bold"))
    attachments = await client.get_resources(course.id)
    if not attachments:
        console.print("[yellow]No downloadable attachments for this course[/]\n")
        action = await _add_nav_keys(
            questionary.select(
                "No resources available:",
                choices=[questionary.Choice(title="Back", value="back")],
                instruction=_NAV_HINT,
            )
        ).ask_async()
        return action == _EXIT

    while True:
        _print_attachments(attachments, title="📎 Course Attachments")
        action = await _add_nav_keys(
            questionary.select(
                "What would you like to do?",
                choices=[
                    questionary.Choice(title="Download files", value="download"),
                    questionary.Choice(title="Back", value="back"),
                ],
                instruction=_NAV_HINT,
            )
        ).ask_async()
        if action == _EXIT:
            return True
        if action in (_BACK, "back", None):
            console.print()
            return False
        if await _prompt_download(client, attachments):
            return True


def _print_assignment_detail(detail: AssignmentDetail) -> None:
    content = _html_to_text(detail.assignment_content) or "No assignment content"
    title = Text(detail.assignment_title)
    console.print(Panel(Text(content), title=title, border_style="cyan"))
    metadata = Text()
    metadata.append("Deadline: ", style="bold")
    metadata.append(detail.assignment_end_time or "—")
    metadata.append("    Assignment ID: ", style="bold")
    metadata.append(detail.id)
    console.print(metadata)
    console.print()


async def browse_assignments(client: BUPTClient, course: Course) -> bool:
    """Browse assignment details and attachments; return True on quit."""
    console.print(Text(f"\n📝 Fetching assignments for [{course.site_name}]...", style="bold"))
    assignments = await client.get_assignments(course.id)
    if not assignments:
        console.print("[yellow]No assignments found for this course[/]\n")
        action = await _add_nav_keys(
            questionary.select(
                "No assignments available:",
                choices=[questionary.Choice(title="Back", value="back")],
                instruction=_NAV_HINT,
            )
        ).ask_async()
        return action == _EXIT

    while True:
        table = Table(title="📝 Assignments", show_lines=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Title", style="cyan")
        table.add_column("Deadline", style="green")
        table.add_column("ID", style="dim")
        for index, assignment in enumerate(assignments, 1):
            table.add_row(
                str(index),
                Text(assignment.assignment_title),
                assignment.assignment_end_time or "—",
                assignment.id,
            )
        console.print(table)

        selected = await _add_nav_keys(
            questionary.select(
                "Select an assignment:",
                choices=[
                    questionary.Choice(
                        title=assignment.assignment_title, value=assignment
                    )
                    for assignment in assignments
                ],
                instruction=_NAV_HINT,
            )
        ).ask_async()
        if selected == _EXIT:
            return True
        if selected in (_BACK, None):
            console.print()
            return False

        detail = await client.get_assignment_detail(selected.id)
        if detail is None:
            continue
        attachments = await client.get_assignment_attachments(detail.resources)

        while True:
            _print_assignment_detail(detail)
            if attachments:
                _print_attachments(
                    attachments,
                    title="📎 Assignment Attachments",
                    show_resource_info=True,
                )
                choices = [
                    questionary.Choice(title="Download files", value="download"),
                    questionary.Choice(title="Back to assignments", value="back"),
                ]
            else:
                console.print("[dim]No downloadable attachments[/]\n")
                choices = [
                    questionary.Choice(title="Back to assignments", value="back")
                ]

            action = await _add_nav_keys(
                questionary.select(
                    "What would you like to do?",
                    choices=choices,
                    instruction=_NAV_HINT,
                )
            ).ask_async()
            if action == _EXIT:
                return True
            if action in (_BACK, "back", None):
                console.print()
                break
            if await _prompt_download(
                client,
                attachments,
                path_components=(course.site_name, detail.assignment_title),
            ):
                return True


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
                for index, course in enumerate(courses, 1):
                    table.add_row(str(index), Text(course.site_name), course.id)
                console.print(table)

                selected = await _add_nav_keys(
                    questionary.select(
                        "Select a course:",
                        choices=[
                            questionary.Choice(title=course.site_name, value=course)
                            for course in courses
                        ],
                        instruction=_NAV_HINT,
                    )
                ).ask_async()
                if not selected or selected in (_BACK, _EXIT):
                    return

                while True:
                    action = await _add_nav_keys(
                        questionary.select(
                            f"Choose content for {selected.site_name}:",
                            choices=[
                                questionary.Choice(
                                    title="Course resources", value="resources"
                                ),
                                questionary.Choice(
                                    title="Assignments", value="assignments"
                                ),
                            ],
                            instruction=_NAV_HINT,
                        )
                    ).ask_async()
                    if action == _EXIT:
                        return
                    if action in (_BACK, None):
                        console.print()
                        break

                    should_exit = (
                        await browse_course_resources(client, selected)
                        if action == "resources"
                        else await browse_assignments(client, selected)
                    )
                    if should_exit:
                        return

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
