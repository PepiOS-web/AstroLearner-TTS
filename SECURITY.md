# Seguridad y privacidad

## Alcance local

El servidor se enlaza a `127.0.0.1:8095`. No incluye autenticación para acceso público ni debe exponerse mediante un proxy abierto, un túnel público o un cambio de dirección de escucha. Los controles de Host y Origin reducen solicitudes ajenas al origen local; no sustituyen el aislamiento del equipo.

La inferencia carga modelos locales. La instalación y las descargas iniciales necesitan Internet. Las utilidades incluidas no envían las grabaciones a un servicio de transcripción externo.

## Datos fuera de Git

El runtime predeterminado está en `%LOCALAPPDATA%\AstroLearnerTTS`, separado del clon. Incluye voces, referencias, transcripciones, textos, resultados, pesos, cachés, logs y copias. `.gitignore` excluye también esos nombres y formatos por si se crean accidentalmente en el clon.

No distribuyas el runtime junto con el código. Revisa los logs y resultados antes de adjuntarlos a una incidencia: pueden contener rutas, nombres de archivos y texto de narración. El repositorio no incluye credenciales, claves de API, tokens ni grabaciones de ejemplo de personas.

## Archivos y procesos

- Importaciones limitadas a 25 MiB y cortes de 5–30 segundos.
- Rutas temporales controladas por UUID, sin aceptar rutas de lectura arbitrarias del cliente.
- Comprobación de cabecera de audio y demultiplexor explícito de FFmpeg.
- Protocolos de lectura restringidos a archivos locales y pipes durante la conversión.
- Descargas WAV limitadas a directorios conocidos del runtime.
- Aprobación de perfiles vinculada a la versión del candidato.
- Un único trabajo de motor a la vez.
- Detención del árbol propio mediante PID, ejecutable, ruta y fecha de creación, para evitar confundir procesos con un PID reutilizado.

Si Windows termina el proceso a la fuerza, pueden quedar temporales. Eso no autoriza borrar carpetas completas sin revisión. Las medidas anteriores son límites de la aplicación, no un análisis antivirus de todo archivo importado.

## Voces y permisos

Usa grabaciones propias o una voz cuyo titular y licencia permitan el uso previsto. Descargar o publicar una narración de otro proveedor no confirma por sí solo permiso para reproducir la identidad vocal en otro sistema. El código no incluye voces comerciales ni autorizaciones personales.

## Informar de un problema

No publiques credenciales, grabaciones ni datos personales en una incidencia. Comunica una descripción mínima y una reproducción con datos sintéticos. Mantén privada la configuración local mientras se revisa un fallo que permita acceso no previsto a archivos o procesos.
