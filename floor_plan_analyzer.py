# ============================================================
# Agepa — Extração de Topologias de Plantas Baixas Arquitetônicas
# Módulo Principal: floor_plan_analyzer.py
# ============================================================
#
# Autor: [Seu Nome]
# TCC — Universidade [Nome]
# Data de Criação: 2026-10-10
#
# Descrição:
#   Módulo de visão computacional que utiliza o modelo
#   fundacional microsoft/Florence-2-base (232M parâmetros)
#   para detectar elementos estruturais em plantas baixas
#   (portas, janelas, paredes) e extrair cotas numéricas
#   via OCR com regiões.
#
# Dependências (pip install):
#   pip install torch transformers Pillow opencv-python einops timm
#
# Tarefas suportadas do Florence-2:
#   <OD>              → Object Detection (portas, janelas, paredes)
#   <OCR_WITH_REGION> → OCR com regiões delimitadas (cotas)
# ============================================================

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import cv2
import numpy as np
import torch
from PIL import Image
from transformers import AutoModelForCausalLM, AutoProcessor

# ── Configuração de Logging ──────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("agepa.floor_plan_analyzer")


# ── Constantes ───────────────────────────────────────────────
MODEL_ID: str = "microsoft/Florence-2-base"
"""Identificador do modelo no Hugging Face Hub (232 M parâmetros)."""

# Paleta de cores BGR para classes detectadas.
# Expandível conforme novas categorias forem adicionadas.
COLOR_PALETTE: Dict[str, Tuple[int, int, int]] = {
    "door":    (0, 200, 80),    # verde
    "window":  (255, 160, 0),   # azul-claro
    "wall":    (0, 80, 255),    # vermelho
    "default": (200, 200, 0),   # ciano
}

# Parâmetros visuais para a renderização dos bounding boxes.
BOX_THICKNESS: int = 2
FONT_SCALE: float = 0.55
FONT_THICKNESS: int = 1
LABEL_PADDING: int = 4


# ── Data Classes ─────────────────────────────────────────────
@dataclass(frozen=True)
class BoundingBox:
    """Representa uma caixa delimitadora [x1, y1, x2, y2] com rótulo.

    Attributes:
        x1: Coordenada x do canto superior-esquerdo.
        y1: Coordenada y do canto superior-esquerdo.
        x2: Coordenada x do canto inferior-direito.
        y2: Coordenada y do canto inferior-direito.
        label: Rótulo textual associado à detecção.
        confidence: Confiança da predição (quando disponível).
    """

    x1: int
    y1: int
    x2: int
    y2: int
    label: str
    confidence: Optional[float] = None

    @property
    def width(self) -> int:
        """Largura da caixa delimitadora em pixels."""
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        """Altura da caixa delimitadora em pixels."""
        return self.y2 - self.y1

    @property
    def area(self) -> int:
        """Área da caixa delimitadora em pixels²."""
        return self.width * self.height

    @property
    def center(self) -> Tuple[int, int]:
        """Centro geométrico (cx, cy) da caixa."""
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    def to_tuple(self) -> Tuple[int, int, int, int]:
        """Retorna as coordenadas como tupla (x1, y1, x2, y2)."""
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass
class DetectionResult:
    """Resultado completo de uma inferência sobre uma planta baixa.

    Attributes:
        task: Prompt de tarefa enviado ao modelo (ex: ``<OD>``).
        image_path: Caminho absoluto da imagem processada.
        raw_output: Dicionário bruto retornado pelo modelo Florence-2.
        boxes: Lista de ``BoundingBox`` extraídas do resultado.
    """

    task: str
    image_path: str
    raw_output: Dict[str, Any] = field(default_factory=dict)
    boxes: List[BoundingBox] = field(default_factory=list)


# ── Classe Principal ─────────────────────────────────────────
class FloorPlanAnalyzer:
    """Analisador de plantas baixas arquitetônicas com Florence-2.

    Gerencia o ciclo completo: carregamento do modelo, inferência
    sobre imagens e pós-processamento com visualização.

    Example:
        >>> analyzer = FloorPlanAnalyzer()
        >>> result = analyzer.run_inference("planta.png", "<OD>")
        >>> analyzer.visualize(result, "output.png")

    Args:
        model_id: Identificador do modelo no HF Hub.
        dtype: Tipo de ponto flutuante dos tensores.
            Padrão ``torch.float16`` para economia de VRAM/RAM.
        device: Dispositivo de execução. Se ``None``, detecta
            automaticamente CUDA → CPU.
    """

    # ── Inicialização ────────────────────────────────────────
    def __init__(
        self,
        model_id: str = MODEL_ID,
        dtype: torch.dtype = torch.float16,
        device: Optional[str] = None,
    ) -> None:
        self._model_id = model_id
        self._dtype = dtype

        # Detecção automática de hardware.
        if device is None:
            self._device: torch.device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu"
            )
        else:
            self._device = torch.device(device)

        logger.info(
            "Dispositivo selecionado: %s  |  dtype: %s",
            self._device,
            self._dtype,
        )

        # Carregamento do modelo e processador.
        self._model: AutoModelForCausalLM = self._load_model()
        self._processor: AutoProcessor = self._load_processor()

    # ── Carregamento do Modelo ───────────────────────────────
    def _load_model(self) -> AutoModelForCausalLM:
        """Carrega o modelo Florence-2-base com otimizações de memória.

        Utiliza ``torch_dtype=float16`` para reduzir o consumo de
        memória em ≈50 % comparado com float32 e ``trust_remote_code``
        para habilitar a arquitetura personalizada do Florence-2.

        Returns:
            Modelo causal carregado e movido ao dispositivo alvo.

        Raises:
            RuntimeError: Falha no download ou carregamento dos pesos.
        """
        logger.info("Carregando modelo '%s' …", self._model_id)

        try:
            model = AutoModelForCausalLM.from_pretrained(
                self._model_id,
                torch_dtype=self._dtype,
                trust_remote_code=True,
            )
            model = model.to(self._device)
            model.eval()  # Modo de avaliação — desativa dropout/batchnorm.

            # Contagem de parâmetros (informativo).
            n_params = sum(p.numel() for p in model.parameters())
            logger.info(
                "Modelo carregado com sucesso: %.1f M parâmetros no %s.",
                n_params / 1e6,
                self._device,
            )
            return model

        except Exception as exc:
            logger.critical(
                "Erro fatal ao carregar o modelo '%s': %s",
                self._model_id,
                exc,
            )
            raise RuntimeError(
                f"Não foi possível carregar o modelo '{self._model_id}'."
            ) from exc

    def _load_processor(self) -> AutoProcessor:
        """Carrega o processador (tokenizer + feature extractor).

        Returns:
            ``AutoProcessor`` configurado para o Florence-2.

        Raises:
            RuntimeError: Falha no download ou inicialização.
        """
        logger.info("Carregando processador …")

        try:
            processor = AutoProcessor.from_pretrained(
                self._model_id,
                trust_remote_code=True,
            )
            logger.info("Processador carregado com sucesso.")
            return processor

        except Exception as exc:
            logger.critical("Erro ao carregar processador: %s", exc)
            raise RuntimeError(
                "Não foi possível carregar o processador."
            ) from exc

    # ── Inferência ───────────────────────────────────────────
    def run_inference(
        self,
        image_path: Union[str, Path],
        task_prompt: str,
        text_input: Optional[str] = None,
    ) -> DetectionResult:
        """Executa a inferência do Florence-2 sobre uma planta baixa.

        Args:
            image_path: Caminho para a imagem da planta baixa.
                Formatos aceitos: PNG, JPG, BMP, TIFF.
            task_prompt: Prompt de tarefa do Florence-2. Valores
                suportados neste módulo:
                - ``"<OD>"`` — Object Detection.
                - ``"<OCR_WITH_REGION>"`` — OCR com regiões.
            text_input: Texto complementar opcional (concatenado ao
                ``task_prompt``). Útil para grounding textual.

        Returns:
            ``DetectionResult`` contendo as caixas extraídas e o
            dicionário bruto de saída do modelo.

        Raises:
            FileNotFoundError: Se ``image_path`` não existir.
            ValueError: Se ``task_prompt`` for inválido.
            RuntimeError: Erro durante a inferência.
        """
        # ── Validações ───────────────────────────────────────
        image_path = Path(image_path)
        if not image_path.is_file():
            raise FileNotFoundError(
                f"Imagem não encontrada: '{image_path}'"
            )

        supported_tasks = {"<OD>", "<OCR_WITH_REGION>"}
        if task_prompt not in supported_tasks:
            logger.warning(
                "Task '%s' não está na lista padrão %s. "
                "Prosseguindo mesmo assim — o Florence-2 pode "
                "suportar tarefas adicionais.",
                task_prompt,
                supported_tasks,
            )

        # ── Carregamento da imagem ───────────────────────────
        try:
            image: Image.Image = Image.open(image_path).convert("RGB")
            logger.info(
                "Imagem carregada: %s  (%d × %d px)",
                image_path.name,
                image.width,
                image.height,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Erro ao abrir a imagem '{image_path}': {exc}"
            ) from exc

        # ── Construção do prompt ─────────────────────────────
        prompt: str = task_prompt
        if text_input:
            prompt = f"{task_prompt} {text_input}"
            logger.info("Prompt composto: '%s'", prompt)

        # ── Pré-processamento (tokenização + pixel values) ───
        inputs = self._processor(
            text=prompt,
            images=image,
            return_tensors="pt",
        )
        # Move todos os tensores ao dispositivo e tipo correto.
        inputs = {
            k: v.to(self._device, dtype=self._dtype)
            if v.dtype.is_floating_point
            else v.to(self._device)
            for k, v in inputs.items()
        }

        # ── Geração (inferência) ─────────────────────────────
        logger.info("Executando inferência com task '%s' …", task_prompt)

        try:
            with torch.inference_mode():
                generated_ids: torch.Tensor = self._model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=1024,
                    num_beams=3,
                    early_stopping=True,
                )
        except Exception as exc:
            raise RuntimeError(
                f"Erro durante a inferência: {exc}"
            ) from exc

        # ── Decodificação ────────────────────────────────────
        generated_text: str = self._processor.batch_decode(
            generated_ids, skip_special_tokens=False
        )[0]

        # O Florence-2 retorna o resultado em formato estruturado.
        # O método ``post_process_generation`` converte o texto
        # gerado em dicionários com bboxes e labels.
        parsed: Dict[str, Any] = self._processor.post_process_generation(
            generated_text,
            task=task_prompt,
            image_size=(image.width, image.height),
        )

        logger.info(
            "Inferência concluída. Chaves retornadas: %s",
            list(parsed.keys()),
        )

        # ── Extração das bounding boxes ──────────────────────
        boxes: List[BoundingBox] = self._extract_boxes(
            parsed, task_prompt
        )
        logger.info("Total de detecções extraídas: %d", len(boxes))

        return DetectionResult(
            task=task_prompt,
            image_path=str(image_path),
            raw_output=parsed,
            boxes=boxes,
        )

    # ── Extração de Bounding Boxes ───────────────────────────
    @staticmethod
    def _extract_boxes(
        parsed_output: Dict[str, Any],
        task_prompt: str,
    ) -> List[BoundingBox]:
        """Converte a saída estruturada do Florence-2 em ``BoundingBox``.

        O formato de saída varia conforme a tarefa:

        **<OD>** → ``parsed_output["<OD>"]`` contém:
            - ``"bboxes"``: lista de ``[x1, y1, x2, y2]``
            - ``"labels"``: lista de strings

        **<OCR_WITH_REGION>** → ``parsed_output["<OCR_WITH_REGION>"]``:
            - ``"quad_boxes"``: lista de polígonos (8 coordenadas)
            - ``"labels"``: lista de textos reconhecidos

        Args:
            parsed_output: Dicionário retornado por
                ``post_process_generation``.
            task_prompt: Tarefa executada (para selecionar o parser).

        Returns:
            Lista de ``BoundingBox`` correspondentes.
        """
        boxes: List[BoundingBox] = []

        if task_prompt == "<OD>":
            od_data = parsed_output.get("<OD>", {})
            bboxes = od_data.get("bboxes", [])
            labels = od_data.get("labels", [])

            for bbox, label in zip(bboxes, labels):
                # bbox = [x1, y1, x2, y2] (floats)
                boxes.append(
                    BoundingBox(
                        x1=int(bbox[0]),
                        y1=int(bbox[1]),
                        x2=int(bbox[2]),
                        y2=int(bbox[3]),
                        label=label,
                    )
                )

        elif task_prompt == "<OCR_WITH_REGION>":
            ocr_data = parsed_output.get("<OCR_WITH_REGION>", {})
            quad_boxes = ocr_data.get("quad_boxes", [])
            labels = ocr_data.get("labels", [])

            for quad, label in zip(quad_boxes, labels):
                # quad_boxes = [x1,y1, x2,y2, x3,y3, x4,y4]
                # Convertemos para bounding box retangular (AABB).
                xs = [quad[i] for i in range(0, 8, 2)]
                ys = [quad[i] for i in range(1, 8, 2)]
                boxes.append(
                    BoundingBox(
                        x1=int(min(xs)),
                        y1=int(min(ys)),
                        x2=int(max(xs)),
                        y2=int(max(ys)),
                        label=label,
                    )
                )

        return boxes

    # ── Visualização com OpenCV ───────────────────────────────
    def visualize(
        self,
        result: DetectionResult,
        output_path: Optional[Union[str, Path]] = None,
        show_window: bool = False,
    ) -> np.ndarray:
        """Desenha bounding boxes e rótulos sobre a imagem original.

        Produz uma imagem anotada com caixas delimitadoras coloridas
        por categoria e rótulos de texto com fundo semi-transparente
        para máxima legibilidade sobre qualquer planta baixa.

        Args:
            result: ``DetectionResult`` retornado por ``run_inference``.
            output_path: Caminho para salvar a imagem anotada.
                Se ``None``, a imagem não é salva em disco.
            show_window: Se ``True``, exibe a imagem em uma janela
                interativa do OpenCV (aguarda tecla para fechar).

        Returns:
            Array NumPy (BGR) da imagem anotada.

        Raises:
            FileNotFoundError: Se a imagem original não existir.
        """
        # Carrega a imagem original via OpenCV (BGR).
        img: np.ndarray = cv2.imread(result.image_path)
        if img is None:
            raise FileNotFoundError(
                f"Não foi possível carregar: '{result.image_path}'"
            )

        # Cria overlay para efeito semi-transparente nos labels.
        overlay: np.ndarray = img.copy()

        for box in result.boxes:
            # Seleciona a cor com base no rótulo (case-insensitive).
            label_lower = box.label.lower()
            color = COLOR_PALETTE.get("default")
            for key, c in COLOR_PALETTE.items():
                if key in label_lower:
                    color = c
                    break

            assert color is not None  # Satisfaz o type-checker.

            # ── Desenha o retângulo ──────────────────────────
            pt1 = (box.x1, box.y1)
            pt2 = (box.x2, box.y2)
            cv2.rectangle(overlay, pt1, pt2, color, BOX_THICKNESS)

            # ── Prepara o texto do rótulo ────────────────────
            display_label: str = box.label
            if box.confidence is not None:
                display_label = f"{box.label} ({box.confidence:.0%})"

            # Calcula o tamanho do texto para o fundo.
            (text_w, text_h), baseline = cv2.getTextSize(
                display_label,
                cv2.FONT_HERSHEY_SIMPLEX,
                FONT_SCALE,
                FONT_THICKNESS,
            )

            # Fundo sólido (retângulo preenchido) atrás do texto.
            label_y_start = max(box.y1 - text_h - 2 * LABEL_PADDING, 0)
            label_y_end = box.y1
            label_x_end = min(
                box.x1 + text_w + 2 * LABEL_PADDING, img.shape[1]
            )

            cv2.rectangle(
                overlay,
                (box.x1, label_y_start),
                (label_x_end, label_y_end),
                color,
                cv2.FILLED,
            )

            # Texto branco sobre o fundo colorido.
            cv2.putText(
                overlay,
                display_label,
                (box.x1 + LABEL_PADDING, box.y1 - LABEL_PADDING),
                cv2.FONT_HERSHEY_SIMPLEX,
                FONT_SCALE,
                (255, 255, 255),
                FONT_THICKNESS,
                cv2.LINE_AA,
            )

        # Blending para suavizar o overlay (alpha = 0.85).
        alpha: float = 0.85
        annotated: np.ndarray = cv2.addWeighted(
            overlay, alpha, img, 1 - alpha, 0
        )

        # ── Salvar em disco ──────────────────────────────────
        if output_path is not None:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(output_path), annotated)
            logger.info("Imagem anotada salva em: %s", output_path)

        # ── Exibir janela interativa ─────────────────────────
        if show_window:
            window_name = f"Agepa — {Path(result.image_path).name}"
            cv2.imshow(window_name, annotated)
            logger.info(
                "Janela aberta. Pressione qualquer tecla para fechar."
            )
            cv2.waitKey(0)
            cv2.destroyAllWindows()

        return annotated

    # ── Utilitários ──────────────────────────────────────────
    def summarize(self, result: DetectionResult) -> str:
        """Gera um resumo textual das detecções para logging/debug.

        Args:
            result: Resultado da inferência.

        Returns:
            String formatada com contagem por categoria.
        """
        from collections import Counter

        counts = Counter(box.label for box in result.boxes)
        lines = [
            f"╔══ Resumo da Análise ══════════════════════╗",
            f"║  Tarefa:     {result.task:<30s}║",
            f"║  Imagem:     {Path(result.image_path).name:<30s}║",
            f"║  Detecções:  {len(result.boxes):<30d}║",
            f"╠═══════════════════════════════════════════╣",
        ]
        for label, count in counts.most_common():
            lines.append(f"║  {label:<20s}  ×{count:<17d}║")
        lines.append(f"╚═══════════════════════════════════════════╝")

        return "\n".join(lines)

    @property
    def device(self) -> torch.device:
        """Dispositivo de execução atual do modelo."""
        return self._device

    @property
    def model_id(self) -> str:
        """Identificador do modelo carregado."""
        return self._model_id


# ══════════════════════════════════════════════════════════════
# Exemplo de Execução
# ══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # ── Configuração ─────────────────────────────────────────
    # Substitua pelo caminho real de uma planta baixa.
    SAMPLE_IMAGE: str = "planta_baixa_exemplo.png"
    OUTPUT_DIR: Path = Path("output")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Agepa — Extrator de Topologias de Plantas Baixas")
    print("  Modelo: microsoft/Florence-2-base (232M params)")
    print("=" * 60)

    # ── Instanciação do Analisador ───────────────────────────
    analyzer = FloorPlanAnalyzer()

    # ── Tarefa 1: Object Detection ───────────────────────────
    # Detecta elementos arquitetônicos (portas, janelas, paredes).
    print("\n▶ Executando Object Detection (<OD>) …")
    try:
        od_result: DetectionResult = analyzer.run_inference(
            image_path=SAMPLE_IMAGE,
            task_prompt="<OD>",
        )

        print(analyzer.summarize(od_result))

        # Salva imagem anotada.
        annotated_od = analyzer.visualize(
            od_result,
            output_path=OUTPUT_DIR / "od_result.png",
            show_window=False,
        )
        print(f"  → Imagem OD salva em: {OUTPUT_DIR / 'od_result.png'}")

    except FileNotFoundError:
        print(
            f"  ⚠ Imagem '{SAMPLE_IMAGE}' não encontrada. "
            f"Coloque uma planta baixa no diretório raiz do projeto."
        )
    except RuntimeError as e:
        print(f"  ✖ Erro na inferência OD: {e}")

    # ── Tarefa 2: OCR com Regiões ────────────────────────────
    # Extrai cotas numéricas (dimensões em metros/centímetros).
    print("\n▶ Executando OCR com Regiões (<OCR_WITH_REGION>) …")
    try:
        ocr_result: DetectionResult = analyzer.run_inference(
            image_path=SAMPLE_IMAGE,
            task_prompt="<OCR_WITH_REGION>",
        )

        print(analyzer.summarize(ocr_result))

        # Imprime as cotas detectadas.
        if ocr_result.boxes:
            print("\n  Cotas / Textos detectados:")
            for i, box in enumerate(ocr_result.boxes, 1):
                print(
                    f"    {i:>3}. \"{box.label}\"  "
                    f"em ({box.x1}, {box.y1}) → ({box.x2}, {box.y2})"
                )

        # Salva imagem anotada.
        annotated_ocr = analyzer.visualize(
            ocr_result,
            output_path=OUTPUT_DIR / "ocr_result.png",
            show_window=False,
        )
        print(f"  → Imagem OCR salva em: {OUTPUT_DIR / 'ocr_result.png'}")

    except FileNotFoundError:
        print(
            f"  ⚠ Imagem '{SAMPLE_IMAGE}' não encontrada. "
            f"Coloque uma planta baixa no diretório raiz do projeto."
        )
    except RuntimeError as e:
        print(f"  ✖ Erro na inferência OCR: {e}")

    print("\n" + "=" * 60)
    print("  Processamento finalizado.")
    print("=" * 60)
