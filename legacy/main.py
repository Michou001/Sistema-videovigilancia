import pathlib
# Solución para compatibilidad de modelos entrenados en Linux/Posix al cargarse en Windows
pathlib.PosixPath = pathlib.WindowsPath

import torch
from PIL import Image
import matplotlib.pyplot as plt
import easyocr
import cv2
from detectarTexto import detectar_texto

# Cargar modelo YOLOv5 entrenado
from pathlib import Path
model = torch.hub.load('yolov5', 'custom',
                       path=str(Path("yolov5/runs/train/exp/weights/best.pt")),
                       force_reload=True, source='local')


#Cargar imagen (Modificar si es necesario)
img = Image.open('imagenes-prueba/img5.jpg')

results = model(img)
results.print()
results.save()

df = results.pandas().xyxy[0]
print(df)

# Verificar si hay detecciones
if len(df) > 0:
    # Tomar la primera detección
    x1, y1, x2, y2 = df.iloc[0][['xmin', 'ymin', 'xmax', 'ymax']].astype(int)

    # obbteniendo region de la placa
    plate_img = img.crop((x1, y1, x2, y2))

    print("TERMINA SCRIPT 1")
    detectar_texto(plate_img)
    plate_img.save("placa_recortada.jpg")

else:
    print("No se detectó ninguna placa en la imagen.")