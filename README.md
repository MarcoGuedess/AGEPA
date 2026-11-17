# AGEPA

Sistema de IA que lê plantas baixas em PDF ou imagem e estima a área construída, os cômodos, o prazo e o custo (CUB) da obra. Usa leitura direta de PDF, OCR híbrido (PP-OCRv6 + Florence-2) e modelos de prazo treinados com obras reais. Projeto de TCC com FastAPI.

## Rodar no computador

```bash
# backend (Python 3.12+); dentro de backend/
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000

# tela; dentro de frontend/
python -m http.server 5500 --bind 127.0.0.1
```

Abra <http://127.0.0.1:5500>. O OCR rápido (PP-OCRv6) é opcional: sem o PaddleOCR instalado, a leitura usa só o Florence-2.

## Deixar online pelo seu PC (Cloudflare Tunnel)

1. Instale o túnel uma vez: `winget install --id Cloudflare.cloudflared`
2. Rode `iniciar_online.bat` (na raiz do projeto). Ele sobe o servidor, que serve a tela e a IA juntas na porta 8765, e abre o túnel.
3. Copie o link `https://….trycloudflare.com` que aparece na janela e compartilhe.

O sistema fica no ar enquanto o PC e a janela estiverem abertos, usando a placa de vídeo do PC. O link muda a cada vez que o túnel é aberto.

A tela publicada no GitHub Pages também funciona com o túnel: abra `https://marcoguedess.github.io/AGEPA/?api=LINK_DO_TUNEL` uma vez e ela passa a usar esse servidor.
