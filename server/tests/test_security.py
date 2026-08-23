from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from server.errors import ApiError
from server.multipart import parse_multipart
from server.security import safe_filename, secure_join, sniff_media, validate_media


class PathSafetyTests(unittest.TestCase):
    def test_filename_discards_client_path_and_control_characters(self) -> None:
        self.assertEqual(safe_filename("../../weird name.png"), "weird_name.png")
        self.assertEqual(safe_filename(r"C:\fake\photo.jpg"), "photo.jpg")
        with self.assertRaises(ApiError):
            safe_filename("bad\x00.png")

    def test_secure_join_blocks_traversal_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(secure_join(root, "safe", "file.png"), root / "safe" / "file.png")
            with self.assertRaises(ApiError):
                secure_join(root, "..", "outside")
            with self.assertRaises(ApiError):
                secure_join(root, "/etc/passwd")


class MediaSafetyTests(unittest.TestCase):
    def test_content_signature_wins_over_extension(self) -> None:
        signature = sniff_media(b"\x89PNG\r\n\x1a\nrest", "malware.exe")
        self.assertIsNotNone(signature)
        self.assertEqual(signature.kind, "image")
        with self.assertRaisesRegex(ApiError, "does not match"):
            validate_media(
                head=b"\x89PNG\r\n\x1a\nrest",
                filename="fake.mp4",
                requested_kind="video",
                size=10,
                limits={"image": 100, "video": 100, "audio": 100},
            )

    def test_unknown_content_is_rejected(self) -> None:
        with self.assertRaisesRegex(ApiError, "not a supported"):
            validate_media(
                head=b"#!/bin/sh",
                filename="image.png",
                requested_kind="auto",
                size=10,
                limits={"image": 100, "video": 100, "audio": 100},
            )


class MultipartTests(unittest.TestCase):
    def test_binary_file_and_text_field_are_streamed(self) -> None:
        boundary = "----h3-test-boundary"
        binary = b"\x00\xff\r\nnot-a-boundary--" + b"x" * 1000
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"kind\"\r\n\r\nvideo\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.mp4\"\r\n"
            "Content-Type: video/mp4\r\n\r\n"
        ).encode() + binary + f"\r\n--{boundary}--\r\n".encode()
        with tempfile.TemporaryDirectory() as directory:
            parts = parse_multipart(
                io.BytesIO(body),
                content_type=f"multipart/form-data; boundary={boundary}",
                content_length=len(body),
                temp_dir=Path(directory),
                max_total_bytes=len(body) + 1,
            )
            self.assertEqual(parts[0].value, b"video")
            self.assertEqual(parts[1].filename, "clip.mp4")
            self.assertEqual(parts[1].temp_path.read_bytes(), binary)
            parts[1].temp_path.unlink()

    def test_total_limit_is_checked_before_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ApiError, "exceeds"):
            parse_multipart(
                io.BytesIO(b"ignored"),
                content_type="multipart/form-data; boundary=x",
                content_length=999,
                temp_dir=Path(directory),
                max_total_bytes=10,
            )


if __name__ == "__main__":
    unittest.main()
