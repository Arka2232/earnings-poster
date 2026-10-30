"""OCR fallback for filings whose embedded text refuses to parse.

Used ONLY when the embedded-text parse (default + layout) yields
None — the common case is a scanned filing or a badly garbled text
layer (FINPIPE, MALUPAPER, BALKRISHNA). It never runs for filings
that already parse, so normal runs stay fast.

Pipeline (per the tested prototype plan):
- Render pages at 300 DPI grayscale with pdftoppm (poppler).
- OCR with tesseract, first psm 6 then psm 4; try parsing each pass.
- If neither pass parses fully, try a row-level merge: start from
  the psm 6 text, and for any key row label whose psm 6 line yields
  too few values, substitute the psm 4 line for the same label.
- Accept a result ONLY if parse_results returns a full dict — the
  parser's own guards (Total cross-check, half-year guard, EPS<->PAT
  sign check, magnitude guard) all still apply. Otherwise None and
  the watcher posts the safe "Results Filed" text as before.

Requires the tesseract + poppler binaries (installed in the workflow
via apt; skipped silently when absent, so a missing binary can never
break a run).
"""

import os
import re
import shutil
import subprocess
import tempfile

from results_parser import parse_results

MAX_PAGES = 25  # results tables sit early; cap keeps rare OCR runs fast
ROW_LABELS = ("revenue from operations", "income from operations",
              "other income", "total income",
              "profit", "loss", "earning per share",
              "earnings per share")


def _binaries_available():
    return shutil.which("pdftoppm") and shutil.which("tesseract")


def _ocr_pages(pdf_bytes, psm):
    """OCR up to MAX_PAGES pages of a PDF, return joined text or None."""
    with tempfile.TemporaryDirectory() as td:
        pdf_path = os.path.join(td, "filing.pdf")
        with open(pdf_path, "wb") as fh:
            fh.write(pdf_bytes)
        r = subprocess.run(
            ["pdftoppm", "-png", "-r", "300", "-gray",
             "-f", "1", "-l", str(MAX_PAGES), pdf_path,
             os.path.join(td, "pg")],
            capture_output=True, timeout=300)
        if r.returncode != 0:
            return None
        pages = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        texts = []
        for pg in pages:
            out = subprocess.run(
                ["tesseract", os.path.join(td, pg), "stdout",
                 "--psm", str(psm)],
                capture_output=True, timeout=180)
            if out.returncode == 0:
                texts.append(out.stdout.decode("utf-8", "replace"))
        if not texts:
            return None
        return "\n".join(texts)


def _value_count(line):
    """Rough count of number-like tokens on a line (merge heuristic)."""
    return len(re.findall(r"\(?-?[\d,]+\.\d+\)?|\(?-?\d[\d,]*\)?", line))


def _merge_texts(base, other):
    """Row-level merge: for any key row label whose base line yields
    fewer than 3 values, substitute the other pass's line for the
    same label (matched by label text, whitespace-insensitive)."""
    base_lines = base.splitlines()
    other_by_label = {}
    for ln in other.splitlines():
        low = re.sub(r"\s+", "", ln.lower())
        for label in ROW_LABELS:
            if label.replace(" ", "") in low:
                other_by_label.setdefault(label, ln)
    merged = []
    for ln in base_lines:
        low = re.sub(r"\s+", "", ln.lower())
        matched = [lb for lb in ROW_LABELS if lb.replace(" ", "") in low]
        if matched and _value_count(ln) < 3:
            sub = other_by_label.get(matched[0])
            if sub is not None and _value_count(sub) >= 3:
                merged.append(sub)
                continue
        merged.append(ln)
    return "\n".join(merged)


def ocr_parse(pdf_bytes):
    """Try to parse a filing via fresh OCR. Returns a parsed dict
    (with "basis") or None. Never raises: any failure means the
    caller falls back to the safe text post."""
    if not _binaries_available():
        return None
    try:
        t6 = _ocr_pages(pdf_bytes, 6)
        if t6:
            parsed = parse_results(t6)
            if parsed is not None:
                return parsed
        t4 = _ocr_pages(pdf_bytes, 4)
        if t4:
            parsed = parse_results(t4)
            if parsed is not None:
                return parsed
        if t6 and t4:
            parsed = parse_results(_merge_texts(t6, t4))
            if parsed is not None:
                return parsed
    except Exception as exc:
        print(f"[ocr] fallback failed ({exc})")
    return None
