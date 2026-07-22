# Offline-first parsing

Status: superseded in part by ADR-0005 and ADR-0006

File2Doc v1 defaults to offline-first parsing: source file content is processed by local parsers such as Miner, local ASR, ffmpeg, LibreOffice, and local OCR rather than remote model or parsing APIs. Remote parsing capabilities may be added later only as explicit opt-in parser capabilities, because the default service must be deployable in privacy-sensitive agent environments and should not send attachment content outside the service boundary unexpectedly.
