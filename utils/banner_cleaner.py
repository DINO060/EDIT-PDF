import io
import os
from pathlib import Path
from typing import List, Tuple

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from PIL import Image
except Exception:
    Image = None

try:
    import imagehash
except Exception:
    imagehash = None

# Optional, used for future improvements; not strictly required
try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    _HAS_CV2 = True
except Exception:
    cv2 = None
    np = None
    _HAS_CV2 = False

SUPPORTED_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _load_reference_hashes(ref_dir: str) -> List[Tuple[str, 'imagehash.ImageHash']]:
    """Load perceptual hashes for all images in ref_dir."""
    refs: List[Tuple[str, 'imagehash.ImageHash']] = []
    if not imagehash or not Image:
        return refs
    p = Path(ref_dir)
    if not p.exists() or not p.is_dir():
        return refs
    for f in sorted(p.iterdir()):
        if f.suffix.lower() in SUPPORTED_IMG_EXT:
            try:
                with Image.open(f) as im:
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    refs.append((str(f), imagehash.phash(im)))
            except Exception:
                continue
    return refs


def _collect_image_rects(page: 'fitz.Page'):
    """Return mapping xref -> bbox for images on the page using rawdict."""
    rects = {}
    try:
        raw = page.get_text("rawdict")
        for b in raw.get("blocks", []):
            if b.get("type") == 1:  # image block
                bbox = b.get("bbox")
                xref = b.get("number")  # PyMuPDF sets image number as xref
                if bbox and xref:
                    rects[int(xref)] = fitz.Rect(bbox)
    except Exception:
        pass
    return rects


def _match_ref(im_bytes: bytes, refs: List[Tuple[str, 'imagehash.ImageHash']], threshold: int) -> bool:
    if not refs or not imagehash or not Image:
        return False
    try:
        with Image.open(io.BytesIO(im_bytes)) as im:
            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")
            h = imagehash.phash(im)
        for _, r in refs:
            # Hamming distance
            if h - r <= threshold:
                return True
    except Exception:
        return False
    return False


def remove_banners_multi(pdf_bytes: bytes, ref_dir: str, threshold: int = 8) -> bytes:
    """
    Remove pages containing banner-like images based on perceptual-hash matches
    to reference images stored in ref_dir.

    - pdf_bytes: input PDF as bytes
    - ref_dir: directory with reference banner images (png/jpg/...)
    - threshold: max Hamming distance for a match (lower is stricter)

    Returns cleaned PDF bytes (or original bytes on failure / missing deps / no refs).
    """
    if not fitz or not Image or not imagehash:
        return pdf_bytes

    refs = _load_reference_hashes(ref_dir)
    if not refs:
        return pdf_bytes

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return pdf_bytes

    pages_to_remove = set()
    try:
        for idx in range(len(doc)):
            try:
                page = doc[idx]
                image_list = page.get_images(full=True)  # tuples; first item is xref
                for info in image_list:
                    xref = int(info[0])
                    try:
                        d = doc.extract_image(xref)
                        im_bytes = d.get("image")
                        if not im_bytes:
                            continue
                        if _match_ref(im_bytes, refs, threshold):
                            pages_to_remove.add(idx)
                            break  # one match is enough to remove the page
                    except Exception:
                        # ignore extraction errors for specific images
                        continue
            except Exception:
                # ignore per-page errors
                continue
    except Exception:
        # scanning failed; fall back to original bytes
        try:
            doc.close()
        except Exception:
            pass
        return pdf_bytes

    if not pages_to_remove:
        try:
            doc.close()
        except Exception:
            pass
        return pdf_bytes

    # Delete pages in descending order to avoid index shifts
    try:
        for p in sorted(pages_to_remove, reverse=True):
            try:
                doc.delete_page(p)
            except Exception:
                # If delete_page unsupported for version, try delete_pages
                try:
                    doc.delete_pages([p])  # may not exist in older versions
                except Exception:
                    # as a last resort, ignore this page
                    pass
        out = io.BytesIO()
        doc.save(out)
        return out.getvalue()
    except Exception:
        return pdf_bytes
    finally:
        try:
            doc.close()
        except Exception:
            pass


def clean_pdf_banners(pdf_bytes: bytes, user_id: int, base_dir: str | Path = "data/banied") -> bytes:
    """
    Convenience wrapper to remove banners using user-specific directory base_dir/{user_id}.
    Returns cleaned bytes or original bytes if nothing to do.
    """
    user_dir = Path(base_dir) / str(user_id)
    return remove_banners_multi(pdf_bytes, str(user_dir))
