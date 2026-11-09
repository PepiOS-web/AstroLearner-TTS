# AstroLearner TTS

Estudio local de diseño de voces y narración en **español e inglés**, con Qwen3-TTS y una GPU NVIDIA. Incluye interfaz web, referencias autorizadas, aprobación de voces, cola de trabajos e historial.

[English overview](docs/README.en.md) · [Arquitectura](docs/IMPLEMENTATION.md) · [Historial](CHANGELOG.md) · [Seguridad y privacidad](SECURITY.md)

Este repositorio contiene **código, pruebas y documentación**. Las grabaciones, voces aprobadas, textos privados, audios generados, modelos descargados y entornos Python permanecen en cada equipo. Una instalación nueva empieza sin muestras ni voces aprobadas.

## Funciones implementadas

- **Diseñar voces:** VoiceDesign crea una identidad original a partir de una descripción y un texto breve.
- **Usar una grabación de referencia:** archivo local, punto inicial, duración y transcripción exacta del corte.
- Candidatos versionados: crear un borrador conserva la voz aprobada; la aprobación corresponde a una versión concreta.
- **Narrar guion:** Base utiliza el audio y la transcripción de la referencia aprobada, por frases y párrafos.
- Una cola con un único trabajo activo, progreso y cancelación entre fragmentos.
- WAV original de 24 kHz y máster separado de 48 kHz / PCM de 24 bits cuando FFmpeg está disponible.
- Historial de los 20 resultados completos más recientes, recuperado después de reiniciar.
- Accesos de escritorio para iniciar/detener y liberación del modelo de la GPU al finalizar cada trabajo.

La interfaz escucha solamente en **http://127.0.0.1:8095/**. La inferencia carga archivos locales y no necesita una clave de API. La instalación inicial requiere Internet para descargar dependencias y modelos.

## Requisitos y configuración validada

- Windows x64, Windows PowerShell y `uv` disponible en PATH.
- Python 3.12, gestionado por `uv` durante la instalación.
- GPU NVIDIA compatible con las ruedas CUDA fijadas. Se verificó una **RTX 5070 de 12 GB**, BF16 y atención SDPA.
- FFmpeg accesible en PATH antes de instalar, para importar grabaciones y producir el máster.
- Espacio para los dos modelos, aproximadamente 9 GB, más el entorno Python, cachés y resultados.

La configuración indicada está probada; no se garantiza compatibilidad con cualquier GPU, sistema operativo o versión de CUDA. La ruta SDPA validada no necesita FlashAttention externo.

## Instalación

Instala previamente [uv desde su documentación oficial](https://docs.astral.sh/uv/getting-started/installation/) y [FFmpeg desde las fuentes indicadas por el proyecto](https://ffmpeg.org/download.html). Comprueba `uv --version` y `ffmpeg -version` en tu terminal.

```powershell
git clone https://github.com/PepiOS-web/AstroLearner-TTS.git
cd AstroLearner-TTS
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Studio.ps1
```

`-ExecutionPolicy Bypass` solo se aplica al proceso mostrado; el instalador no cambia la política global de PowerShell.

El instalador crea un entorno aislado, instala las versiones de `requirements.lock`, descarga las revisiones de `models.lock.json`, copia la aplicación al runtime y crea los accesos **AstroLearner TTS** y **Detener AstroLearner TTS** en el escritorio.

Runtime predeterminado: `%LOCALAPPDATA%\AstroLearnerTTS`, fuera del clon y separado del servidor HomeLab.

Si FFmpeg no está en PATH, el instalador avisa. Puede seguir siendo posible generar el WAV original, pero la importación de grabaciones requiere FFmpeg y el máster no estará disponible. El instalador no descarga ni redistribuye ese ejecutable.

## Primer uso

1. Abre **AstroLearner TTS** desde el escritorio.
2. Selecciona **Narrador español** o **English storyteller**.
3. En **Diseñar voces**, genera una muestra original; o utiliza **Usar una grabación de referencia** con audio propio o autorizado.
4. Escucha el candidato completo y pulsa **Aprobar esta voz** si te convence.
5. Abre **Narrar guion**, pega el texto separado en párrafos y genera la narración.
6. Escucha y descarga **Audio original** y **Máster**. Revisa nombres, cifras, acento y continuidad.

En una referencia, la transcripción debe corresponder exactamente al corte elegido. Puedes seleccionar un archivo largo y usar inicio/duración para escoger el fragmento. El formulario utiliza incrementos de 0,1 segundos.

Al terminar, usa **Detener AstroLearner TTS**. Cerrar la pestaña no detiene el proceso. Los lanzadores también pueden ejecutarse desde su ubicación instalada:

```powershell
& "$env:LOCALAPPDATA\AstroLearnerTTS\Start-Studio.ps1"
& "$env:LOCALAPPDATA\AstroLearnerTTS\Stop-Studio.ps1"
```

Los archivos Start/Stop de la raíz del repositorio son plantillas que copia el instalador; deben ejecutarse desde el runtime instalado.

## Límites actuales

| Operación | Límite |
|---|---|
| Diseño de muestra | 500 caracteres |
| Archivo de referencia | 25 MiB; MP3, WAV, FLAC, M4A u OGG |
| Fragmento de referencia | Entre 5 y 30 segundos |
| Transcripción de referencia | 500 caracteres |
| Narración por trabajo | 18.000 caracteres |
| Frase individual | 500 caracteres; se rechaza sin truncarla si los supera |
| Trabajos pendientes/activos | Hasta 8; una inferencia a la vez |
| Historial restaurado | Los 20 resultados completos más recientes |

Introduce los guiones largos por partes. La duración depende del texto y la voz; tampoco se garantiza inferencia en tiempo real. Cancelar espera al final de la llamada de inferencia actual. Los tiempos guardados son de fragmentos, no alineación por palabra ni subtítulos listos para publicar.

## Acento, interpretación y calidad

Qwen permite seleccionar `Spanish`, sin un selector regional `es-ES` en estos modelos. La dirección del perfil español pide un acento peninsular concreto, pero ese texto no garantiza el resultado. Una referencia autorizada y bien transcrita permite comparar identidades y acentos mediante una prueba real.

**Dirección de voz** se usa en VoiceDesign. En la narración Base, la identidad procede de la referencia aprobada. La semilla controla parte del muestreo, no selecciona el acento ni aumenta por sí sola la calidad.

El original conserva la salida de 24 kHz. El máster se remuestrea a 48 kHz y normaliza en dos pasadas hacia -16 LUFS y -1,5 dBTP. Ese procesado no crea detalle adicional ni corrige la interpretación.

La configuración actual divide alrededor de 250 caracteres y añade 120 ms entre fragmentos o 200 ms al finalizar un párrafo. Se han propuesto comparaciones con bloques más largos y pausas basadas en el silencio existente: **esas mejoras de naturalidad siguen pendientes**. No hay entrenamiento integrado ni aprendizaje automático por usar la aplicación.

## Datos y actualizaciones

| Carpeta del runtime | Contenido local |
|---|---|
| `.venv` | Python y dependencias |
| `app` | Código desplegado |
| `models` | Pesos y registro de revisiones |
| `profiles` | Candidatos, referencias, transcripciones e índice de aprobación |
| `jobs` | Textos, fragmentos, originales, másteres y resultados |
| `incoming` | Archivos temporales de importación |
| `logs` | Diagnóstico del servidor |

Para actualizar, detén el estudio, actualiza el clon y ejecuta:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Install-Studio.ps1 -SkipModels
```

El instalador conserva modelos, perfiles y trabajos. Respalda `profiles` y `jobs` por separado; no los añadas a Git. El estudio no se configura para arrancar automáticamente con Windows ni recibir conexiones desde otros equipos.

## Utilidades opcionales

- `scripts/create_first_samples.py`: genera dos candidatos sintéticos originales, sin aprobarlos.
- `scripts/transcribe_reference.py`: transcripción local en CPU con Whisper-small fijado a una revisión; admite `--language spanish` y `--language english`, hasta los primeros 30 segundos, sin cambiar perfiles.
- `scripts/preview_reference.py`: primera audición independiente de un perfil todavía sin aprobar; exige detener antes el estudio.

Ejemplo de transcripción con un archivo propio:

```powershell
$ttsPython = "$env:LOCALAPPDATA\AstroLearnerTTS\.venv\Scripts\python.exe"
& $ttsPython .\scripts\transcribe_reference.py `
  --runtime "$env:LOCALAPPDATA\AstroLearnerTTS" `
  --input "C:\audio\referencia-propia.wav" `
  --output "$env:LOCALAPPDATA\AstroLearnerTTS\checks\transcripcion.json" `
  --language spanish
```

La utilidad descarga el modelo ASR oficial si falta; el audio se procesa localmente. Revisa su transcripción, especialmente nombres, cifras y límites de frase.

## Pruebas

Las pruebas usan referencias sintéticas temporales y motores simulados; no requieren voces privadas ni descargar Qwen. Entorno CPU independiente:

```powershell
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-test.txt
& .\.venv\Scripts\python.exe -m pytest -q
```

La revisión inicial incluye 69 pruebas de fragmentación, audio, perfiles, aprobación por candidato, cola, cancelación, historial, importación, acceso local y límites de archivos. Se comprobaron aparte generaciones reales de diseño y narración en ambos idiomas con la GPU indicada. Las pruebas simuladas no evalúan naturalidad ni parecido de voz.

## Modelos y licencias

| Modelo | Función |
|---|---|
| [Qwen3-TTS VoiceDesign 1.7B](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign) | Diseñar una voz original |
| [Qwen3-TTS Base 1.7B](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base) | Narrar con audio y transcripción de referencia |
| [Whisper-small](https://huggingface.co/openai/whisper-small) | Transcripción opcional local |

Las revisiones TTS están en `models.lock.json`; la revisión ASR está fijada en su script. Los modelos tienen sus propias licencias y no se incluyen en Git. Este código usa [MIT](LICENSE); esa licencia no concede derechos sobre voces, grabaciones o ejecutables externos.
