"""Test every configured vision/text model ONE BY ONE and print why each works or fails.

    python -m scripts.check_llm                       # uses a generated test receipt
    python -m scripts.check_llm --image C:\\path\\receipt.jpg
    python -m scripts.check_llm --text                # also test the text models

Reads the same .env as the app. Run it first when the bot says it "couldn't read" a receipt.
"""
import argparse
import asyncio
import io
import time

from PIL import Image, ImageDraw, ImageFont

from app.ai.llm_provider import Candidate, _build_messages, build_chat_model, get_candidates
from app.ai.llm_utils import classify_error, extract_json, message_text, prepare_image_for_llm
from app.ai.receipt_extractor import EXTRACTION_SYSTEM, _categorizer_system
from app.config.settings import settings
from app.schemas.expense import CategorizationResult, ReceiptExtraction


def sample_receipt() -> bytes:
    img = Image.new("RGB", (700, 420), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=34)
    except TypeError:  # old Pillow
        font = ImageFont.load_default()
    for i, line in enumerate(["ABC RESTAURANT", "Invoice No: 1042", "Date: 08/10/2026",
                              "Team Dinner", "Subtotal  2300.00", "GST  150.00", "TOTAL  Rs 2450.00"]):
        draw.text((40, 25 + i * 52), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def try_model(cand: Candidate, system: str, user_text: str, schema, image=None):
    started = time.perf_counter()
    try:
        model = build_chat_model(cand)
        response = await asyncio.wait_for(
            model.ainvoke(_build_messages(system, user_text, image)), timeout=settings.LLM_TIMEOUT_SECONDS + 10)
        parsed = schema.model_validate(extract_json(message_text(response)))
        print(f"  OK    {cand.key}  ({time.perf_counter() - started:.1f}s)  -> {parsed.model_dump(mode='json')}")
        return True
    except Exception as exc:  # noqa: BLE001
        kind = "timeout" if isinstance(exc, asyncio.TimeoutError) else classify_error(exc)
        print(f"  FAIL  {cand.key}  [{kind}]  {str(exc)[:220]!r}")
        return False


async def main(image_path, test_text):
    print("Providers with keys:",
          [p for p in settings.provider_order
           if {"openrouter": settings.OPENROUTER_API_KEY, "gemini": settings.gemini_key,
               "huggingface": settings.hf_token}.get(p)] or "NONE - check your .env")
    raw = open(image_path, "rb").read() if image_path else sample_receipt()
    jpeg = prepare_image_for_llm(raw, settings.IMAGE_MAX_SIDE_PX, settings.IMAGE_JPEG_QUALITY)
    print(f"Test image: {len(jpeg) // 1024} KB after resize\n\nVISION models (receipt reading):")
    vision = await get_candidates(vision=True)
    ok = [await try_model(c, EXTRACTION_SYSTEM, "Extract the receipt details as JSON.", ReceiptExtraction, jpeg)
          for c in vision]
    print(f"\n=> {sum(ok)}/{len(ok)} vision models usable")
    if test_text:
        print("\nTEXT models (categorization):")
        text = await get_candidates(vision=False)
        ok = [await try_model(c, _categorizer_system(), "Vendor: ABC Restaurant\nItems: Team dinner\nAmount: Rs 2450",
                              CategorizationResult) for c in text]
        print(f"\n=> {sum(ok)}/{len(ok)} text models usable")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", help="path to a real receipt image")
    ap.add_argument("--text", action="store_true", help="also test text models")
    args = ap.parse_args()
    asyncio.run(main(args.image, args.text))
