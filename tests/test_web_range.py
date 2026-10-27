import asyncio
import base64
from pathlib import Path
import tempfile
import unittest

from tdl_watcher.web_range import BLOCK_SIZE, RangeFailure, download_ranges, fetch_block


class FakePage:
    def __init__(self, content, fail_at=None):
        self.content = content
        self.fail_at = fail_at
        self.calls = []

    async def evaluate(self, script, args, **kwargs):
        offset, length = args["offset"], args["length"]
        self.calls.append(offset)
        if offset == self.fail_at:
            raise OSError("temporary network failure")
        end = offset + length - 1
        return {"status": 206, "range": f"bytes {offset}-{end}/{len(self.content)}",
                "bytes": base64.b64encode(self.content[offset:offset + length]).decode()}


class Progress:
    def update(self, *args, **kwargs):
        pass


class RangeTests(unittest.TestCase):
    def test_interruption_resumes_verified_blocks_without_restarting(self):
        async def run():
            content = b"a" * BLOCK_SIZE + b"b" * BLOCK_SIZE + b"end"
            meta = {"id": "991", "size": len(content), "dc": 2}
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "out"
                output.mkdir()
                checkpoints = root / "checkpoints"
                page = FakePage(content, fail_at=BLOCK_SIZE)
                with self.assertRaisesRegex(RangeFailure, "progress retained"):
                    await download_ranges(page, meta, output, checkpoints, "-123", "55", "file.rar", Progress())
                self.assertEqual(page.calls, [0, BLOCK_SIZE])
                self.assertEqual(list(output.iterdir()), [])
                page = FakePage(content)
                result = await download_ranges(page, meta, output, checkpoints, "-123", "55", "file.rar", Progress())
                self.assertEqual(page.calls, [BLOCK_SIZE, 2 * BLOCK_SIZE])
                self.assertEqual(result.read_bytes(), content)
                self.assertEqual(list(checkpoints.iterdir()), [])
        asyncio.run(run())

    def test_corrupt_checkpoint_is_not_resumed(self):
        async def run():
            content = b"a" * BLOCK_SIZE + b"tail"
            meta = {"id": "991", "size": len(content), "dc": 2}
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "out"
                output.mkdir()
                checkpoints = root / "checkpoints"
                with self.assertRaises(RangeFailure):
                    await download_ranges(FakePage(content, fail_at=BLOCK_SIZE), meta, output, checkpoints,
                                          "-123", "55", "file.rar", Progress())
                (checkpoints / ".tdl-123-55.part").write_bytes(b"x" * BLOCK_SIZE)
                page = FakePage(content)
                with self.assertRaisesRegex(RangeFailure, "checkpoint is damaged"):
                    await download_ranges(page, meta, output, checkpoints, "-123", "55", "file.rar", Progress())
                self.assertEqual(page.calls, [])
        asyncio.run(run())

    def test_rejects_incorrect_content_range(self):
        class WrongRange(FakePage):
            async def evaluate(self, script, args, **kwargs):
                return {"status": 206, "range": "bytes 0-4/6", "bytes": "aGVsbG8="}
        async def run():
            with self.assertRaisesRegex(RangeFailure, "requested byte range"):
                await fetch_block(WrongRange(b"hello"), {"id": "1", "size": 5, "dc": 2}, 0, 5)
        asyncio.run(run())
