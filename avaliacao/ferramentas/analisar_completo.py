"""Envia uma planta ao servidor e guarda a resposta COMPLETA (ambientes, textos lidos...).

uso: analisar_completo.py arquivo saida.json [pagina]
"""
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
src, out = Path(sys.argv[1]), Path(sys.argv[2])
mime = "application/pdf" if src.suffix.lower() == ".pdf" else "image/png"
boundary = uuid.uuid4().hex
CRLF = "\r\n"
head = (f"--{boundary}{CRLF}Content-Disposition: form-data; name=\"file\"; filename=\"{src.name}\"{CRLF}"
        f"Content-Type: {mime}{CRLF}{CRLF}")
body = head.encode() + src.read_bytes() + CRLF.encode()
if len(sys.argv) > 3:
    # "4" → uma página; "todas", "1-6", "2,4" → prédio (várias páginas)
    field = "pagina" if sys.argv[3].isdigit() else "paginas"
    body += (f"--{boundary}{CRLF}Content-Disposition: form-data; name=\"{field}\"{CRLF}{CRLF}"
             f"{sys.argv[3]}{CRLF}").encode()
body += f"--{boundary}--{CRLF}".encode()
import os
req = urllib.request.Request(os.environ.get("AGEPA_URL", "http://127.0.0.1:8000") + "/api/analisar", data=body, method="POST",
                             headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=5400) as r:
        data = json.load(r)
except urllib.error.HTTPError as exc:
    print("HTTP", exc.code, exc.read().decode("utf-8", "replace")[:500])
    sys.exit(1)
for k in ("annotated_image_b64", "original_image_b64"):
    data.pop(k, None)
out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"{src.name} p.{sys.argv[3] if len(sys.argv) > 3 else '-'}: {data.get('area_m2')} m² "
      f"({data.get('area_kind')}, {data.get('area_method')}, {data.get('confidence')}) · "
      f"ambientes com área {data.get('rooms_with_area')} · {time.time() - t0:.0f} s")
