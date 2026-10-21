"""2.26.1: сканер на iPhone — ZXing (static/vendor). Файл тот же, что в npm (sha256), отдаётся программой без
интернета и на самом деле читает штрихкод ТТН Новой Почты (Code 128) с картинки."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

STATIC = Path(__file__).parent.parent / "promloader" / "static"
VENDOR = STATIC / "vendor"
ZXING = VENDOR / "zxing-0.21.3.min.js"
FIXTURE = Path(__file__).parent / "fixtures" / "ttn_code128.png"
NODE = shutil.which("node")


def test_vendored_zxing_is_untouched_and_documented():
    readme = (VENDOR / "README.md").read_text(encoding="utf-8")
    expected = re.search(r"sha256 `([0-9a-f]{64})`", readme).group(1)
    assert hashlib.sha256(ZXING.read_bytes()).hexdigest() == expected
    assert (VENDOR / "zxing-LICENSE.txt").read_text().lstrip().startswith("Apache License")
    assert ZXING.stat().st_size < 400 * 1024


def test_scanner_loads_zxing_from_the_program(client):
    js = (STATIC / "scanner.js").read_text(encoding="utf-8")
    assert '"/static/vendor/zxing-0.21.3.min.js"' in js  # не с CDN: склад без интернета тоже сканирует
    r = client.get("/static/vendor/zxing-0.21.3.min.js")
    assert r.status_code == 200 and r.content == ZXING.read_bytes()


@pytest.mark.skipif(not NODE, reason="нужен node")
def test_zxing_reads_nova_poshta_ttn_barcode():
    img = Image.open(FIXTURE).convert("L")
    w, h = img.size
    script = f"""
const Z = require({json.dumps(str(ZXING))});
const gray = Uint8ClampedArray.from(require("fs").readFileSync(0));  // серые пиксели — из stdin
const source = new Z.RGBLuminanceSource(gray, {w}, {h});
const hints = new Map([[Z.DecodeHintType.POSSIBLE_FORMATS, [Z.BarcodeFormat.CODE_128]],
                       [Z.DecodeHintType.TRY_HARDER, true]]);
const reader = new Z.MultiFormatReader();
reader.setHints(hints);
const r = reader.decodeWithState(new Z.BinaryBitmap(new Z.HybridBinarizer(source)));
console.log(JSON.stringify({{text: r.getText(), format: Z.BarcodeFormat[r.getBarcodeFormat()]}}));
"""
    out = subprocess.run([NODE, "-e", script], input=img.tobytes(), capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()[-1500:]
    assert json.loads(out.stdout) == {"text": "20451560371674", "format": "CODE_128"}
