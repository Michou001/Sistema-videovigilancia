import pathlib
# Solución para compatibilidad de modelos entrenados en Linux/Posix al cargarse en Windows
pathlib.PosixPath = pathlib.WindowsPath

import cv2
import torch
import numpy as np
from PIL import Image
import easyocr
import re
import csv
import datetime
import time
from pathlib import Path

# Patrones regex de formatos de placas
PATRONES_PLACAS = [
    r"^[A-Z]{3}([-]|[ -]| [- ]|[ - ])\d{3}$",
    r"^[A-Z]{2}([-]|[ -]| [- ]|[ - ])\d{4}$",
    r"^[A-Z]{1}([-]|[ -]| [- ]|[ - ])\d{4}$",
    r"^[A-Z]{3}([-]|[ -]| [- ]|[ - ])\d{3}([-]|[ -]| [- ]|[ - ])[A-Z]{1}$",  # edomex
    r"^[A-Z]{3}([-]|[ -]| [- ]|[ - ])\d{2}([-]|[ -]| [- ]|[ - ])\d{2}$",  # guanajuato
    r"^(CD|CC)([-]|[ -]| [- ]|[ - ])\d{3,4}$",
    r"^(OF|OFICIAL)([-]|[ -]| [- ]|[ - ])\d+$",
    r"^[A-Za-z0-9]+-[A-Za-z0-9]+-[A-Za-z0-9]+$",
    r"^[A-Za-z0-9]+-[A-Za-z0-9]+$",
]

def guardar_placa_en_csv(placa, archivo_csv="placas_detectadas.csv"):
    with open(archivo_csv, mode="a", newline="", encoding="utf-8") as file:
        hora_actual = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        writer = csv.writer(file)
        writer.writerow([hora_actual, placa])
        print(f"[GUARDADO] Placa '{placa}' registrada en {archivo_csv} a las {hora_actual}")

def verificar_placa(texto):
    texto_limpio = texto.strip().upper()
    for patron in PATRONES_PLACAS:
        if re.match(patron, texto_limpio):
            return True, texto_limpio
    return False, texto_limpio

def main():
    print("Iniciando componentes...")

    # 1. Cargar modelo YOLOv5
    pesos_path = Path("yolov5/runs/train/exp/weights/best.pt")
    if not pesos_path.exists():
        print(f"Error: No se encontró el modelo en {pesos_path}")
        return

    print("Cargando modelo YOLOv5...")
    model = torch.hub.load(
        'yolov5',
        'custom',
        path=str(pesos_path),
        force_reload=False,
        source='local'
    )
    # Umbral de confianza mínimo de detección
    model.conf = 0.45

    # 2. Inicializar EasyOCR (se inicializa una sola vez para evitar lentitud)
    print("Inicializando EasyOCR...")
    use_gpu = torch.cuda.is_available()
    reader = easyocr.Reader(['es', 'en'], gpu=use_gpu)

    # 3. Inicializar Cámara
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: No se pudo acceder a la cámara web.")
        return

    print("\n" + "="*50)
    print("Cámara iniciada. Presiona 'q' o 'ESC' para salir.")
    print("="*50 + "\n")

    # Registro en memoria para evitar guardar la misma placa repetidas veces seguidas
    # Formato: { "PLACA": timestamp_ultimo_registro }
    placas_recientes = {}
    COOLDOWN_SEGUNDOS = 15  # Tiempo antes de volver a registrar la misma placa

    # Contador de frames para no saturar el OCR
    frame_count = 0
    OCR_INTERVAL = 3  # Ejecutar OCR cada N frames si se detecta placa
    
    ultimo_texto_detectado = ""
    ultimo_tiempo_texto = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Error al recibir fotograma de la cámara.")
            break

        frame_count += 1
        # Convertir BGR a RGB para YOLO / PIL
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(frame_rgb)

        # Inferencia con YOLOv5
        results = model(img_pil)
        detections = results.pandas().xyxy[0]

        # Si hay detecciones de placa
        if len(detections) > 0:
            for _, row in detections.iterrows():
                x1, y1, x2, y2 = int(row['xmin']), int(row['ymin']), int(row['xmax']), int(row['ymax'])
                conf = float(row['confidence'])

                # Asegurar coordenadas válidas dentro del frame
                h, w, _ = frame.shape
                x1, y1 = max(0, x1), max(0, y1)
                x2, y2 = min(w, x2), min(h, y2)

                # Dibujar bounding box de la placa
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame,
                    f"Placa: {conf:.2f}",
                    (x1, max(20, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 255, 0),
                    2
                )

                # Ejecutar OCR solo cada N frames para mantener fluidez
                if frame_count % OCR_INTERVAL == 0 and (x2 - x1 > 20) and (y2 - y1 > 10):
                    # Recortar la región de la placa
                    crop_bgr = frame[y1:y2, x1:x2]

                    if crop_bgr.size > 0:
                        ocr_results = reader.readtext(crop_bgr)

                        for (_, texto, ocr_conf) in ocr_results:
                            if ocr_conf > 0.35:
                                es_valida, placa_formateada = verificar_placa(texto)
                                
                                ahora = time.time()
                                ultimo_texto_detectado = f"{placa_formateada} ({ocr_conf:.2f})"
                                ultimo_tiempo_texto = ahora

                                print(f"Texto OCR: '{texto}' | Confianza: {ocr_conf:.2f} | Formato válido: {es_valida}")

                                if es_valida:
                                    # Verificar si ya se guardó recientemente
                                    ultimo_guardado = placas_recientes.get(placa_formateada, 0)
                                    if ahora - ultimo_guardado > COOLDOWN_SEGUNDOS:
                                        guardar_placa_en_csv(placa_formateada)
                                        placas_recientes[placa_formateada] = ahora
                                        # Guardar imagen recortada de la última placa detectada
                                        cv2.imwrite("placa_recortada.jpg", crop_bgr)

        # Mostrar último texto detectado en pantalla si fue reciente (< 3 segundos)
        if time.time() - ultimo_tiempo_texto < 3.0:
            cv2.rectangle(frame, (10, 10), (400, 50), (0, 0, 0), -1)
            cv2.putText(
                frame,
                f"OCR: {ultimo_texto_detectado}",
                (15, 38),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 255),
                2
            )

        # Mostrar el frame en tiempo real
        cv2.imshow("Detector de Matriculas en Tiempo Real - YOLOv5 + EasyOCR", frame)

        # Salir con la tecla 'q' o ESC
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q') or key == 27:
            break

    cap.release()
    cv2.destroyAllWindows()
    print("Cámara cerrada correctamente.")

if __name__ == "__main__":
    main()
