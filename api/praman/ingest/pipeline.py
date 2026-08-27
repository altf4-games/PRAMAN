"""Ingest orchestration — the `make ingest` entrypoint. Reads raw inputs
(HTML, CSV, images) from `api/praman/seed/raw/`, sends each through
`extract.py` (a live LLM call), runs the result through `normalise.py`
(deterministic), and writes a structured catalog. One bad source file
doesn't take down the run — its error is collected and reported instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from praman.adapters.llm import LLMClient, get_llm_client
from praman.config import get_settings
from praman.ingest.extract import (
    ExtractedProduct,
    IngestExtractionError,
    extract_from_image,
    extract_from_text,
)
from praman.ingest.normalise import normalise_batch

_TEXT_SUFFIXES = {".html", ".htm", ".csv", ".txt"}
_IMAGE_MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}

# Restocking cost isn't something a model reads off a price list — it's a
# merchant-economics assumption. Deterministic default by category_class,
# overridable later once a merchant states their actual policy.
DEFAULT_RESTOCKING_COST_PCT: dict[str, float] = {
    "perishable": 0.0,
    "consumable": 0.0,
    "digital": 0.0,
    "durable": 0.10,
    "service": 0.05,
    "bespoke": 0.30,
}


@dataclass
class IngestResult:
    products: list[ExtractedProduct] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def needs_review_count(self) -> int:
        return sum(1 for p in self.products if p.needs_review)


async def ingest_file(llm: LLMClient, path: Path) -> list[ExtractedProduct]:
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        text = path.read_text(encoding="utf-8")
        return await extract_from_text(
            llm,
            text,
            source="csv" if suffix == ".csv" else "vlm",
            source_hint=f"{suffix.lstrip('.')} file named {path.name}",
        )
    if suffix in _IMAGE_MIME_BY_SUFFIX:
        image_bytes = path.read_bytes()
        return await extract_from_image(
            llm,
            image_bytes,
            mime_type=_IMAGE_MIME_BY_SUFFIX[suffix],
            source_hint=f"photograph named {path.name}",
            source_media_url=str(path),
        )
    raise IngestExtractionError(f"unrecognised file type: {path.name}")


async def ingest_directory(llm: LLMClient, directory: Path) -> IngestResult:
    result = IngestResult()
    for path in sorted(directory.iterdir()):
        if path.is_dir():
            continue
        try:
            products = await ingest_file(llm, path)
        except IngestExtractionError as exc:
            result.errors.append(f"{path.name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — one flaky source must not sink the batch
            result.errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        result.products.extend(products)

    result.products = normalise_batch(result.products)
    return result


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "item"


def to_catalog_entry(product: ExtractedProduct) -> dict[str, object]:
    return {
        "sku": slugify(product.name),
        "name": product.name,
        "category": product.category,
        "category_class": product.category_class,
        "unit_price_paise": product.unit_price_paise,
        "stock": product.stock,
        "return_window_days": product.return_window_days,
        "fulfilment_hours": product.fulfilment_hours,
        "restocking_cost_pct": DEFAULT_RESTOCKING_COST_PCT[product.category_class],
        "is_personalised": product.is_personalised,
        "field_confidence": product.field_confidence,
        "needs_review": product.needs_review,
        "source": product.source,
        "source_media_url": product.source_media_url,
    }


async def build_catalog_from_csv(llm: LLMClient, csv_path: Path) -> IngestResult:
    text = csv_path.read_text(encoding="utf-8")
    try:
        products = await extract_from_text(
            llm, text, source="csv", source_hint=f"CSV file named {csv_path.name}"
        )
    except IngestExtractionError as exc:
        return IngestResult(errors=[f"{csv_path.name}: {exc}"])
    except Exception as exc:  # noqa: BLE001 — surface as a result error, not a crash
        return IngestResult(errors=[f"{csv_path.name}: {type(exc).__name__}: {exc}"])
    return IngestResult(products=normalise_batch(products))


def _print_summary(label: str, result: IngestResult) -> None:
    print(f"\n=== {label} ===")
    print(
        f"extracted: {len(result.products)}  needs_review: {result.needs_review_count}  errors: {len(result.errors)}"
    )
    for err in result.errors:
        print(f"  ! {err}")
    for p in result.products:
        flag = "REVIEW" if p.needs_review else "ok"
        print(
            f"  [{flag}] {p.name} — {p.category} ({p.category_class}) ₹{p.unit_price_paise / 100:.2f}"
        )


async def _main_async(args: argparse.Namespace) -> None:
    settings = get_settings()
    llm = get_llm_client(settings)

    if args.mode == "raw":
        raw_dir = Path(args.path)
        result = await ingest_directory(llm, raw_dir)
        _print_summary(f"raw ingest: {raw_dir}", result)
        if args.out:
            Path(args.out).write_text(
                json.dumps([to_catalog_entry(p) for p in result.products], indent=2)
            )
    elif args.mode == "catalog":
        csv_path = Path(args.path)
        result = await build_catalog_from_csv(llm, csv_path)
        _print_summary(f"catalog build: {csv_path}", result)
        out_path = Path(args.out) if args.out else csv_path.with_suffix(".json")
        out_path.write_text(json.dumps([to_catalog_entry(p) for p in result.products], indent=2))
        print(f"wrote {out_path} ({len(result.products)} items)")


def main() -> None:
    parser = argparse.ArgumentParser(description="PRAMAN catalog ingest")
    parser.add_argument("mode", choices=["raw", "catalog"])
    parser.add_argument("path", help="raw/ directory (mode=raw) or a master CSV (mode=catalog)")
    parser.add_argument("--out", help="output JSON path")
    args = parser.parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
    sys.exit(0)
