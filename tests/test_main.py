from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from main import (
    BUPTClient,
    AssignmentResource,
    Session,
    _build_download_dir,
    _html_to_text,
    _safe_filename,
    _safe_path_component,
    _unique_destination,
)


def make_client(handler: httpx.MockTransport) -> BUPTClient:
    client = BUPTClient()
    client._session = Session(access_token="token", user_id="user-1", expires_at=1)
    client._client = httpx.AsyncClient(transport=handler)
    return client


class TextAndPathTests(unittest.TestCase):
    def test_html_to_text_retains_links_and_images(self) -> None:
        content = (
            "<p>Read <a href='https://example.test/submit'>the requirements</a>"
            "<br><img src='https://example.test/image.png'></p>"
            "<script>ignored()</script>"
        )

        self.assertEqual(
            _html_to_text(content),
            "Read the requirements (https://example.test/submit)\n"
            "[Image: https://example.test/image.png]",
        )

    def test_safe_paths_and_nested_assignment_directory(self) -> None:
        filename = _safe_filename("../../report?.pdf")
        self.assertNotIn("/", filename)
        self.assertNotIn("?", filename)
        self.assertTrue(filename.endswith(".pdf"))
        self.assertEqual(_safe_path_component("CON"), "_CON")

        result = _build_download_dir(
            Path("/tmp/downloads"), ("Course/A", "Final: report")
        )
        self.assertEqual(result, Path("/tmp/downloads/Course_A/Final_ report"))

    def test_unique_destination_checks_disk_and_current_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir)
            (destination / "report.pdf").touch()
            reserved = {destination / "report (1).pdf"}

            result = _unique_destination(destination, "report.pdf", reserved)

            self.assertEqual(result, destination / "report (2).pdf")


class AssignmentAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_assignment_list_uses_expected_payload_and_paginates(self) -> None:
        payloads: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/ykt-site/work/student/list")
            payload = json.loads(request.content)
            payloads.append(payload)
            current = payload["current"]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "records": [
                            {
                                "id": f"assignment-{current}",
                                "assignmentTitle": f"Assignment {current}",
                                "assignmentEndTime": "2026-07-18 23:59",
                            }
                        ],
                        "pages": 2,
                    }
                },
            )

        client = make_client(httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)

        assignments = await client.get_assignments("course-1")

        self.assertEqual([item.id for item in assignments], ["assignment-1", "assignment-2"])
        self.assertEqual([payload["current"] for payload in payloads], [1, 2])
        self.assertEqual(payloads[0]["siteId"], "course-1")
        self.assertEqual(payloads[0]["userId"], "user-1")
        self.assertEqual(payloads[0]["size"], 5)
        self.assertEqual(payloads[0]["status"], 0)
        self.assertIsNone(payloads[0]["studentAssignmentStatus"])

    async def test_detail_and_resource_resolution(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ykt-site/work/detail":
                self.assertEqual(request.url.params["assignmentId"], "assignment-1")
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "id": "assignment-1",
                            "assignmentTitle": "Final",
                            "assignmentContent": "<p>Details</p>",
                            "assignmentEndTime": "2026-07-18 23:59",
                            "assignmentResource": [
                                {
                                    "resourceId": "resource-1",
                                    "resourceName": "template.doc",
                                    "resourceType": "doc",
                                },
                                {
                                    "resourceId": "missing",
                                    "resourceName": "missing.pdf",
                                    "resourceType": "pdf",
                                },
                                {"resourceId": "invalid"},
                            ],
                        }
                    },
                )
            self.assertEqual(request.url.path, "/blade-source/resource/list/byId")
            resource_id = request.url.params["resourceIds"]
            if resource_id != "resource-1":
                return httpx.Response(503)
            records = [
                {
                    "id": "resource-1",
                    "name": "template.doc",
                    "fileSizeUnit": "58.5KB",
                    "ext": "doc",
                    "storageId": "storage-1",
                }
            ]
            return httpx.Response(200, json={"data": records})

        client = make_client(httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)

        detail = await client.get_assignment_detail("assignment-1")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(len(detail.resources), 2)
        attachments = await client.get_assignment_attachments(detail.resources)

        self.assertEqual(detail.assignment_title, "Final")
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].resource_id, "resource-1")
        self.assertEqual(attachments[0].size, "58.5KB")
        self.assertEqual(
            attachments[0].url,
            "https://fileucloud.bupt.edu.cn/ucloud/document/storage-1.doc",
        )

    async def test_null_resource_list_is_treated_as_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "assignment-1",
                        "assignmentTitle": "No files",
                        "assignmentResource": None,
                    }
                },
            )

        client = make_client(httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)

        detail = await client.get_assignment_detail("assignment-1")

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail.resources, [])

    async def test_attachment_without_suffix_uses_resource_extension(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "resource-1",
                            "name": "template",
                            "ext": "docx",
                            "storageId": "storage-1",
                        }
                    ]
                },
            )

        client = make_client(httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)
        resources = [
            AssignmentResource(
                resourceId="resource-1",
                resourceName="template",
                resourceType="docx",
            )
        ]

        attachments = await client.get_assignment_attachments(resources)

        self.assertEqual(attachments[0].filename, "template.docx")

    async def test_existing_course_resource_mapping_is_preserved(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.path, "/ykt-site/site-resource/tree/student"
            )
            self.assertEqual(request.url.params["siteId"], "course-1")
            self.assertEqual(request.url.params["userId"], "user-1")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "resourceName": "Week 1",
                            "attachmentVOs": [
                                {
                                    "resource": {
                                        "name": "slides.pdf",
                                        "url": "https://example.test/slides.pdf",
                                        "fileSizeUnit": "1MB",
                                    }
                                }
                            ],
                        }
                    ]
                },
            )

        client = make_client(httpx.MockTransport(handler))
        self.addAsyncCleanup(client.client.aclose)

        attachments = await client.get_resources("course-1")

        self.assertEqual(attachments[0].name, "[Week 1] slides.pdf")
        self.assertEqual(attachments[0].download_name, "slides.pdf")


if __name__ == "__main__":
    unittest.main()
