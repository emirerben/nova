"""Offline API contracts. Run with unittest without provider credentials."""

import base64
import io
import unittest
import wave

from app.services.credit_plan import CreditAllocation, comparison_credit_preflight
from app.services.generated_asset_contracts import (
    ImageRequest,
    NarrationRequest,
    decode_image,
    decode_narration,
    image_payload,
    narration_events,
)


class CreditPlanContracts(unittest.TestCase):
    def test_planning_ceiling_is_not_authorization(self):
        allocation = CreditAllocation()
        self.assertEqual(allocation.ceiling("openai", "evaluation"), 40)
        self.assertEqual(allocation.ceiling("anthropic", "evaluation"), 60)
        self.assertFalse(allocation.authorizes_live_calls)
        with self.assertRaises(ValueError):
            allocation.ceiling("anthropic", "image")

    def test_reject_invalid_allocations(self):
        for value in (-1, float("nan"), float("inf"), 131):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CreditAllocation(openai_evaluation=value)

    def test_existing_preflight_groups_by_provider(self):
        packet = {
            "repetitions": 2,
            "arms": [
                {"id": "a", "model": "gpt-6.1-sol"},
                {"id": "b", "model": "claude-sonnet-5-5"},
                {"id": "c", "model": "gemini-3.1-pro-preview"},
            ],
        }
        prepared = {
            "rows": [
                {"arm": "a", "estimated_max_usd": 1},
                {"arm": "b", "estimated_max_usd": 2},
                {"arm": "c", "estimated_max_usd": 3},
            ],
            "estimated_max_usd": 12,
        }
        result = comparison_credit_preflight(packet, prepared=prepared)
        self.assertEqual(
            result["provider_estimated_max_usd"], {"openai": 2, "anthropic": 4, "google": 6}
        )
        self.assertEqual(result["live_authorized"], False)
        self.assertEqual(result["quality_verdict"], "unassessed")
        self.assertEqual(result["unfunded_providers"], ["google"])
        self.assertEqual(result["allocation_status"], "blocked")
        self.assertNotIn("rows", result)

    def test_reject_unknown_models_and_overallocation(self):
        for model, cost in (("gpt-unpriced", 1), ("gpt-6.1-sol", 41), ("gpt-6.1-sol", -1)):
            with self.subTest(model=model, cost=cost), self.assertRaises(ValueError):
                comparison_credit_preflight(
                    {"repetitions": 1, "arms": [{"id": "a", "model": model}]},
                    prepared={
                        "rows": [{"arm": "a", "estimated_max_usd": cost}],
                        "estimated_max_usd": cost,
                    },
                )

    def test_narration_and_images_use_supported_workflows(self):
        image = image_payload(
            ImageRequest(prompt="A watercolor background", editable_title="Merhaba")
        )
        self.assertEqual(image["model"], "gpt-image-2.5-sunburst")
        self.assertNotIn("Merhaba", image["prompt"])
        self.assertEqual(image["n"], 1)
        self.assertNotIn("background", image)
        cutout = image_payload(ImageRequest(prompt="A football icon", transparent_background=True))
        self.assertEqual(cutout["background"], "transparent")
        self.assertEqual(cutout["output_format"], "png")
        events = narration_events(NarrationRequest(approved_script="Merhaba dünya."))
        self.assertEqual(events[0]["session"]["model"], "gpt-realtime-2.1-mini")
        self.assertEqual(events[-1]["response"]["conversation"], "none")
        self.assertEqual(events[-1]["response"]["input"][0]["content"][0]["text"], "Merhaba dünya.")

    def test_input_bounds(self):
        for request in (
            ImageRequest(prompt=""),
            ImageRequest(prompt="x", size="auto"),
            ImageRequest(prompt="x", quality="auto"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                image_payload(request)
        for request in (
            NarrationRequest(approved_script=""),
            NarrationRequest(approved_script="x", voice="cloned"),
        ):
            with self.subTest(request=request), self.assertRaises(ValueError):
                narration_events(request)

    def test_image_response_requires_png_and_usage(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (1024, 1536)).save(buf, format="PNG")
        response = {
            "data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }
        request = ImageRequest(prompt="Background", editable_title="Merhaba")
        asset = decode_image(response, request)
        self.assertEqual(asset.mime_type, "image/png")
        self.assertEqual(asset.editable_title, "Merhaba")
        with self.assertRaises(ValueError):
            decode_image(response, ImageRequest(prompt="Background", size="1024x1024"))
        self.assertEqual(asset.content, buf.getvalue())
        self.assertEqual(asset.usage["total_tokens"], 30)
        for invalid in (
            {**response, "usage": None},
            {**response, "data": [{"url": "https://example.test/image.png"}]},
            {**response, "data": response["data"] * 2},
            {**response, "data": [{"b64_json": base64.b64encode(b"not an image").decode()}]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                decode_image(invalid, request)

    def test_corrupt_png_pixels_are_rejected(self):
        import struct
        import zlib

        from PIL import Image

        output = io.BytesIO()
        Image.new("RGB", (1024, 1536)).save(output, format="PNG")
        png = output.getvalue()
        offset = 8
        corrupted = png[:8]
        while offset < len(png):
            size = struct.unpack(">I", png[offset : offset + 4])[0]
            kind = png[offset + 4 : offset + 8]
            data = png[offset + 8 : offset + 8 + size]
            if kind == b"IDAT":
                data = b"not-zlib"
            corrupted += (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(">I", zlib.crc32(kind + data))
            )
            offset += size + 12
        response = {
            "data": [{"b64_json": base64.b64encode(corrupted).decode()}],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }
        with self.assertRaises(ValueError):
            decode_image(response, ImageRequest(prompt="Background"))

    def test_audio_and_transcript_must_belong_to_same_content(self):
        for field, value in (("item_id", "other"), ("output_index", 1), ("content_index", 1)):
            events = self.audio_events()
            events[3][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_narration(events, NarrationRequest(approved_script="Merhaba dünya."))

    def audio_events(self):
        return [
            {"type": "response.created", "response": {"id": "resp_1"}},
            {
                "type": "response.output_audio.delta",
                "response_id": "resp_1",
                "item_id": "item_1",
                "output_index": 0,
                "content_index": 0,
                "delta": base64.b64encode(b"\0\0" * 240).decode(),
            },
            {
                "type": "response.output_audio.done",
                "response_id": "resp_1",
                "item_id": "item_1",
                "output_index": 0,
                "content_index": 0,
            },
            {
                "type": "response.output_audio_transcript.done",
                "response_id": "resp_1",
                "item_id": "item_1",
                "output_index": 0,
                "content_index": 0,
                "transcript": "Merhaba dünya.",
            },
            {
                "type": "response.done",
                "response": {
                    "id": "resp_1",
                    "status": "completed",
                    "usage": {"total_tokens": 30, "input_tokens": 10, "output_tokens": 20},
                },
            },
        ]

    def test_completed_narration_becomes_wav(self):
        asset = decode_narration(
            self.audio_events(), NarrationRequest(approved_script="Merhaba dünya.")
        )
        with wave.open(io.BytesIO(asset.content)) as audio:
            self.assertEqual(audio.getframerate(), 24000)
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getnframes(), 240)
        self.assertEqual(asset.mime_type, "audio/wav")
        self.assertEqual(asset.provider_request_id, "resp_1")

    def test_reject_incomplete_or_wrong_script_audio(self):
        for removed in range(5):
            events = self.audio_events()
            del events[removed]
            with self.subTest(removed=removed), self.assertRaises(ValueError):
                decode_narration(events, NarrationRequest(approved_script="Merhaba dünya."))
        with self.assertRaises(ValueError):
            decode_narration(self.audio_events(), NarrationRequest(approved_script="Buy now!"))
        events = self.audio_events()
        events[-1]["response"]["status"] = "cancelled"
        with self.assertRaises(ValueError):
            decode_narration(events, NarrationRequest(approved_script="Merhaba dünya."))
        events = self.audio_events()
        events[1]["response_id"] = "other"
        with self.assertRaises(ValueError):
            decode_narration(events, NarrationRequest(approved_script="Merhaba dünya."))


if __name__ == "__main__":
    unittest.main()
