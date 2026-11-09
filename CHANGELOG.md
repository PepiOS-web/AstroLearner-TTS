# Historial de desarrollo

## 0.1.0 — Repositorio independiente, 2026-11-09

Primera publicación del estudio TTS como proyecto separado de HomeLab. La aplicación se ejecuta en el PC Windows y no modifica los servicios del servidor doméstico.

### 2026-11-07 — Motor local y primeras pruebas

- Entorno Python 3.12 aislado con versiones de dependencias fijadas.
- Qwen3-TTS VoiceDesign y Base 1.7B descargados en revisiones concretas.
- BF16 y SDPA comprobados con una RTX 5070 de 12 GB.
- Perfiles separados de narración española e inglesa.
- Primeras muestras originales; aprobación explícita antes de usar la identidad en narraciones normales.
- Conservación del WAV original y exportación de un máster de sonoridad separado.

### 2026-11-08 — Estudio, referencias y persistencia

- Interfaz local de diseño, narración, reproducción y descargas.
- Cola serializada con progreso y cancelación entre fragmentos.
- Referencias versionadas: un candidato nuevo no reemplaza una voz aprobada.
- Audio de perfil vinculado al identificador exacto; rechazo de referencias obsoletas.
- Recuperación de los 20 trabajos completos más recientes al reiniciar.
- Lanzadores de Windows con identidad de proceso comprobada y cierre del árbol propio.
- Importación de grabaciones locales con límites de tamaño, formato, duración y transcripción.
- Conversión del fragmento a mono 24 kHz, conservación del archivo aportado y registro de hash y corte.
- Validación de silencio, avisos de nivel/saturación y limpieza acotada de temporales.
- Transcripción local opcional con Whisper-small y audiciones independientes de preparación.

### 2026-11-09 — Inglés y documentación reproducible

- Selección explícita de español o inglés en la utilidad de transcripción.
- Preparación de referencias inglesas con cortes entre frases y transcripción correspondiente.
- Documentación de los límites reales de VoiceDesign, Base, acento, semilla y máster.
- Separación del código de todos los audios, voces, modelos, cachés y datos del runtime.
- Entorno de pruebas CPU independiente de Torch, Qwen y grabaciones privadas.

### Pendiente, no implementado

- Comparación controlada de naturalidad con otras referencias.
- Evaluación de fragmentos de 400–450 caracteres y unión según los silencios existentes.
- Entrenamiento o fine-tuning de una voz: actualmente se realiza inferencia por referencia.
- Alineación por palabra, subtítulos y síntesis en tiempo real garantizada.

La calidad perceptiva debe evaluarse escuchando textos nuevos. Una propuesta pendiente no implica que se haya aplicado un cambio ni entrenado un modelo.
