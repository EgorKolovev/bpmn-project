"""Extract plain text from the two customer PDFs."""
from pypdf import PdfReader
import re

def extract(path: str) -> str:
    reader = PdfReader(path)
    chunks = []
    for page in reader.pages:
        t = page.extract_text() or ""
        chunks.append(t)
    raw = "\n".join(chunks)
    # Collapse runs of whitespace but keep paragraph breaks
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    # Strip the <b>BPMN diagram:</b> markers — they're rendering artifacts
    raw = re.sub(r"<b>BPMN diagram:\s*</b>[^\n]*", "", raw)
    return raw.strip()

for name, path in [
    ("PDF1 (Служебная командировка)", "/tmp/pdf1_komandirovka.pdf"),
    ("PDF2 (Отправка документов)", "/tmp/pdf2_otpravka.pdf"),
]:
    text = extract(path)
    print(f"=== {name} ===")
    print(f"length: {len(text)} chars, {len(text.split())} words")
    print(f"first 500 chars: {text[:500]}")
    print()
    with open(f"{path}.txt", "w", encoding="utf-8") as f:
        f.write(text)
