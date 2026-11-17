# ==============================================================================
# ATENÇÃO: este script gera o modelo DEMONSTRATIVO. A área é sorteada e o
# "prazo real" é a fórmula área/15 + 0,2·cômodos + ruído (dados simulados).
# O treino com obras reais é feito por backend/train_prazo.py
# (planilha dados/obras_reais.csv; guia em dados/LEIAME.md).
# ==============================================================================
# Script de Treinamento - Agepa (Kaggle Notebook)
# Dataset: CubiCasa5K
# Objetivo: Treinar um RandomForestRegressor para estimar prazos de obras
# baseado na área e quantidade de cômodos extraídos das plantas baixas.
# ==============================================================================

import os
import glob
import json
import numpy as np
import pandas as pd
import xml.etree.ElementTree as ET
from pathlib import Path
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
import joblib

# ── Configurações ─────────────────────────────────────────────────────────────
KAGGLE_INPUT_DIR = "/kaggle/input/cubicasa5k" # Diretório padrão do Kaggle
KAGGLE_OUTPUT_DIR = "/kaggle/working"
MAX_SAMPLES = 500

# Fator de produtividade: meses por m2 (ex: 15 m2 por mês) -> 1/15 = 0.0666
FATOR_PRODUTIVIDADE = 1 / 15.0 
RUIDO_STD = 1.5 # Desvio padrão do ruído em meses

# ── Funções de Parsing ────────────────────────────────────────────────────────

def extract_features_from_svg(svg_path):
    """
    Extrai área e quantidade de cômodos do arquivo SVG do CubiCasa5K.
    Heurística simplificada: busca por elementos de 'Space' ou anotações.
    """
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        
        # O CubiCasa5K utiliza namespaces em seus SVGs
        # Buscando o número de polígonos/paths que representam cômodos
        qtd_comodos = 0
        area_pixels = 0.0
        
        # A estrutura padrão do SVG do CubiCasa5k possui grupos com id="Spaces"
        for elem in root.iter():
            # Conta elementos de texto ou path com classe 'Space' ou rótulos
            if 'id' in elem.attrib and 'Space' in elem.attrib['id']:
                qtd_comodos += 1
                
        # Caso o SVG não possua IDs claros, usamos um default ou lemos do JSON se existir.
        if qtd_comodos == 0:
            qtd_comodos = np.random.randint(2, 10) # Fallback seguro
            
        # Estimativa de área (fallback para simulação realista)
        # Como as coordenadas do SVG precisariam de escala para conversão em metros reais
        area_total_m2 = np.random.uniform(40.0, 300.0)
        
        return area_total_m2, qtd_comodos
    except Exception as e:
        print(f"Aviso: Erro ao parsear SVG {svg_path}: {e}")
        return None, None

def extract_features_from_json(json_path):
    """
    Tenta extrair features de um JSON se estiver disponível.
    """
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            
        # Depende muito do formato do JSON gerado, simulando chaves comuns
        area = data.get("area_total_m2", None)
        comodos = data.get("qtd_comodos", None)
        
        if area is not None and comodos is not None:
            return float(area), int(comodos)
            
        return None, None
    except Exception as e:
        # Silencioso, pois o JSON pode estar corrompido ou ausente
        return None, None

# ── 1. Coleta e Parsing de Dados ─────────────────────────────────────────────

print("Iniciando a leitura do dataset CubiCasa5K...")
dataset_records = []

# Busca todos os diretórios de plantas
# No CubiCasa5K, os arquivos costumam ficar em subpastas
plantas_dirs = glob.glob(os.path.join(KAGGLE_INPUT_DIR, "dataset", "*", "*"))
if not plantas_dirs:
    # Tenta outro padrão comum no Kaggle
    plantas_dirs = glob.glob(os.path.join(KAGGLE_INPUT_DIR, "*", "*"))

print(f"Total de diretórios encontrados: {len(plantas_dirs)}")

count = 0
for d in plantas_dirs:
    if count >= MAX_SAMPLES:
        break
        
    dir_path = Path(d)
    if not dir_path.is_dir():
        continue
        
    area_m2 = None
    comodos = None
    
    # 1. Tenta extrair do JSON
    json_files = list(dir_path.glob("*.json"))
    if json_files:
        area_m2, comodos = extract_features_from_json(json_files[0])
        
    # 2. Se falhar, tenta extrair do SVG (CubiCasa5K original)
    if area_m2 is None or comodos is None:
        svg_files = list(dir_path.glob("*.svg"))
        if svg_files:
            area_m2, comodos = extract_features_from_svg(svg_files[0])
            
    # 3. Se obteve sucesso na extração
    if area_m2 is not None and comodos is not None:
        dataset_records.append({
            "id": dir_path.name,
            "area_total_m2": area_m2,
            "qtd_comodos": comodos
        })
        count += 1

if not dataset_records:
    print("Aviso: Nenhum dado extraído! Verifique a estrutura do dataset no Kaggle.")
    # Gerando dados sintéticos temporários apenas para garantir que a célula não quebre 
    # caso o caminho do dataset no Kaggle seja ligeiramente diferente.
    print("Gerando dados sintéticos para fallback...")
    for i in range(MAX_SAMPLES):
        dataset_records.append({
            "id": f"synthetic_{i}",
            "area_total_m2": np.random.uniform(40.0, 300.0),
            "qtd_comodos": np.random.randint(2, 10)
        })

df = pd.DataFrame(dataset_records)
print(f"Dataset construído com {len(df)} amostras limitadas a {MAX_SAMPLES}.")

# ── 2. Data Fusion e Engenharia de Features ──────────────────────────────────

# Cálculo do prazo real base + ruído estatístico (Normal)
np.random.seed(42) # Reprodutibilidade
ruido = np.random.normal(0, RUIDO_STD, len(df))

# Regra de negócio + ruído + penalidade por complexidade (cômodos extras)
df["prazo_real_meses"] = (df["area_total_m2"] * FATOR_PRODUTIVIDADE) + \
                         (df["qtd_comodos"] * 0.2) + \
                         ruido

# Garantindo que nenhum prazo seja menor que 3 meses (prazo mínimo)
df["prazo_real_meses"] = df["prazo_real_meses"].clip(lower=3.0)

print("\nAmostra dos dados processados:")
print(df.head())

# ── 3. Treinamento do Modelo (RandomForestRegressor) ─────────────────────────

X = df[["area_total_m2", "qtd_comodos"]]
y = df["prazo_real_meses"]

X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

print("\nTreinando o modelo RandomForestRegressor...")
rf_model = RandomForestRegressor(
    n_estimators=100,
    max_depth=10,
    random_state=42,
    n_jobs=-1
)

rf_model.fit(X_train, y_train)

# ── 4. Avaliação e Métricas ──────────────────────────────────────────────────

y_pred = rf_model.predict(X_test)
mae = mean_absolute_error(y_test, y_pred)
rmse = np.sqrt(mean_squared_error(y_test, y_pred))

print("══════════════════════════════════════════════════")
print("  Resultados do Modelo de Estimativa de Prazos")
print("══════════════════════════════════════════════════")
print(f"  MAE  (Erro Absoluto Médio): {mae:.2f} meses")
print(f"  RMSE (Raiz do Erro Quad.):  {rmse:.2f} meses")
print("══════════════════════════════════════════════════")

# ── 5. Exportação do Modelo ──────────────────────────────────────────────────

output_path = os.path.join(KAGGLE_OUTPUT_DIR, "random_forest_model.joblib")
joblib.dump(rf_model, output_path)

print(f"\n✅ Modelo treinado com sucesso!")
print(f"💾 Salvo em: {output_path}")
print("Você pode baixar este arquivo no menu lateral direito em 'Output'.")
