# Placas mexicanas

Cómo lee, corrige y describe el sistema las placas de México, y cómo afinar el
OCR con placas de tus propias cámaras.

Todo lo de esta página vive en [`shared/plates.py`](../shared/plates.py) (lo usan
el worker y la API) y está cubierto por `tests/test_placas_mexicanas.py` y
`tests/test_placas_api.py`.

---

## Formatos reconocidos

La [NOM-001-SCT-2-2016](https://www.dof.gob.mx/nota_detalle.php?codigo=5442476&fecha=24/06/2016)
define la conformación de caracteres por tipo de vehículo y servicio. Además
siguen en circulación placas de la norma anterior y formatos que cada entidad
emite en la práctica. El sistema conoce todos estos:

| Tipo | Formato (L = letra, D = dígito) | Ejemplo | Norma |
|---|---|---|---|
| Automóvil particular | `LLL-DDD-L` | `PZW-123-A` | vigente |
| Automóvil particular, CDMX | `LDD-LLL` | `A01-AAA` | vigente |
| Automóvil particular | `LLL-DD-DD` | `ABC-12-34` | anterior |
| Automóvil | `LLL-DDD`, `DDD-LLL` | `ABC-123` | anterior |
| Camión particular | `L-DDD-LL`, `LL-DDDD-L`, `LL-DD-DDD` | `A-123-BC` | vigente / práctica |
| Autotransporte federal | `DD-LL-DL` | `01-AB-2C` | vigente |
| Taxi / servicio público | `L-DDD-LLL`, `DD-DD-LLL` | `B-123-CDE` | vigente / práctica |
| Taxi y transporte público, CDMX | `L-DDDD-L`, `L-DDDDD`, `DDD-L-DDD` | `L-0001-A` | vigente |
| Motocicleta | `LDD-LL`, `LDDD-L`, `LLL-DD`, `DDDD-L` (CDMX) | `N01-AB` | vigente / práctica |
| Autobús, remolque | varios | `12-ABC-34` | vigente / práctica |
| Especiales | demostración, auto antiguo, discapacidad, ecológico, policía, protección civil (`DD-PC-DDD`), ambulancia (`AM-DDD-LL`), bomberos (`BM-DDD-LL`), diplomático (`CD-DDDD`), consular (`CC-DDDD`), dolly (`D-DD-DDD`) | | |

Los **formatos raros** (especiales, remolques, autobuses) se aceptan si la
lectura encaja tal cual, pero el sistema no "corrige" una lectura dudosa hacia
ellos y exige confianza alta para emitir el evento: con tantas plantillas,
casi cualquier ruido encajaría en alguna. Ejemplo medido: `PZW-123-A` mal
leída como `6ZW123` encaja en "demostración".

## La regla que más ayuda: sin I, Ñ, O ni Q

La norma vigente dice: *"en los caracteres alfabéticos no deberán utilizarse
las letras I, Ñ, O, Q"*. Consecuencias en la lectura:

- Una **O** o una **Q** leídas donde va una letra de una placa vigente son en
  realidad una **D**: `POW-123-A` se corrige a `PDW-123-A`.
- Un **0** donde va una letra es una **D**, no una O.
- Un **1** donde va una letra no se corrige: ninguna letra permitida se le
  parece lo bastante para adivinarla.
- Al dar de alta una placa en la lista negra, si trae I, O o Q en un formato
  vigente, el sistema lo explica y sugiere la corrección.

## Entidad por serie

En el formato particular vigente (`LLL-DDD-L`), las tres primeras letras dicen
qué entidad emitió la placa. La tabla de series viene del Apéndice C de la
norma (reproducida en la tabla de series por entidad de *Vehicle registration
plates of Mexico*, Wikipedia; coincide con otras transcripciones de la norma):

| Entidad | Serie | Entidad | Serie |
|---|---|---|---|
| Aguascalientes | AAA–AFZ | Morelos | PVA–RDZ |
| Baja California | AGA–CYZ | Nayarit | REA–RJZ |
| Baja California Sur | CZA–DEZ | Nuevo León | RKA–TGZ |
| Campeche | DFA–DKZ | Oaxaca | THA–TMZ |
| Chiapas | DLA–DSZ | Puebla | TNA–UJZ |
| Chihuahua | DTA–ETZ | Querétaro | UKA–UPZ |
| Coahuila | EUA–FPZ | Quintana Roo | URA–UVZ |
| Colima | FRA–FWZ | San Luis Potosí | UWA–VEZ |
| Durango | FXA–GFZ | Sinaloa | VFA–VSZ |
| Guanajuato | GGA–GYZ | Sonora | VTA–WKZ |
| Guerrero | GZA–HFZ | Tabasco | WLA–WWZ |
| Hidalgo | HGA–HRZ | Tamaulipas | WXA–XSZ |
| Jalisco | HSA–LFZ | Tlaxcala | XTA–XXZ |
| Estado de México | LGA–PEZ | Veracruz | XYA–YVZ |
| Michoacán | PFA–PUZ | Yucatán | YWA–ZCZ |
| | | Zacatecas | ZDA–ZHZ |

La Ciudad de México usa su propio formato (`LDD-LLL`), así que se reconoce por
el formato. En los formatos anteriores **no** se adivina la entidad.

La alerta lo usa para decir qué buscar:
*"Placa GHT-903-K en lista negra: Robado · Vehículo gris (color aprox.) ·
Automóvil particular de Guanajuato"*.

## Placa delantera o trasera (F / T)

La norma imprime en chico una **F** (frontal) o una **T** (trasera). Si el OCR
la alcanza a leer como un carácter más al inicio o al final (`PZW123AT`), se
quita. Una T que sí es parte de la placa (`PZW-123-T`) no se toca: la lectura
que ya es válida tal cual siempre gana.

## Placas extranjeras

El OCR (modelos `*-v2-global`) también dice de qué país parece la placa.
**Medido con placas mexicanas, contesta "United States" con frecuencia**
(fondo blanco y letras negras se parecen). Por eso:

- Una lectura con **formato mexicano válido siempre se trata como mexicana**,
  diga lo que diga el país del OCR (queda en `meta.pais_ocr` solo como dato).
- Solo si la lectura **no** encaja en ningún formato mexicano y el OCR
  reconoce otro país con claridad (≥ 75% de los votos del vehículo), se
  reporta como placa extranjera, **tal cual se leyó**. Antes esas placas se
  descartaban; en una ciudad fronteriza eran muchas.
- En la lista negra se puede dar de alta una placa extranjera marcando
  «placa extranjera»: no se valida contra los formatos mexicanos.

## Corregir una lectura

En **Registro**, cada placa tiene un lápiz (rol operador o superior). Al
corregir:

1. El evento queda con el valor correcto y conserva la lectura original.
2. Se vuelve a cruzar contra la lista negra: si la placa correcta está
   buscada, sale la alerta en ese momento.
3. La corrección queda en la bitácora y la lectura se conserva
   `RETENCION_CORREGIDOS_DIAS` (180 por defecto) como dato de entrenamiento.

## Afinar el OCR con placas de tus cámaras

El OCR se entrenó con placas de muchos países. Lo que más lo mejora aquí son
placas mexicanas de estas mismas cámaras (su ángulo, su luz, su compresión).

1. Junta correcciones durante unas semanas (con cientos ya se nota).
2. Descarga el dataset: **Administración → Reentrenamiento**, o
   `python tools/exportar_dataset.py [--automaticas]`.
   Trae `anotaciones.csv` (`image_path, plate_text, plate_region`) en el
   formato de [fast-plate-ocr](https://github.com/ankandrew/fast-plate-ocr) y
   los recortes justos de cada placa (no la foto con margen).
3. En una máquina con GPU:

   ```bash
   pip install "fast-plate-ocr[train]"
   # separa ~15% de las filas en validacion.csv
   fast_plate_ocr train \
       --model-config-file cct_s_v2.yaml --plate-config-file placas_mx.yaml \
       --annotations anotaciones.csv --val-annotations validacion.csv --epochs 50
   fast_plate_ocr export --model modelo.keras --format onnx
   ```

   Parte del modelo global (`cct-s-v2-global-model`) para no perder lo que ya
   sabe, y valida contra lecturas que el modelo actual falla.
4. Apunta `PLATE_OCR` del `.env` del worker al modelo nuevo y compara con
   `python tools/calibrar_distancia.py` antes de dejarlo en producción.

Las lecturas "automáticas" dan volumen, pero pueden traer errores del propio
OCR: revisa una muestra antes de entrenar con ellas.

> El ZIP son placas de vehículos reales (datos personales). La descarga queda en
> la bitácora y el archivo ya no lo purga el sistema: guárdalo cifrado y bórralo
> al terminar.
