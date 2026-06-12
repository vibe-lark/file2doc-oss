# MinerU pipeline as the v1 primary parser

Status: superseded by ADR-0005

File2Doc v1 uses MinerU `pipeline` as the primary parser for offline document parsing, invoked through the MinerU CLI from the File2Doc worker. This keeps the first implementation offline-first, compatible with pure CPU Kubernetes pods, and aligned with the result package contract that needs structured Markdown, page-aware blocks, tables, images, formulas, coordinates, and parser artifacts. MarkItDown may be evaluated later as a lightweight text-oriented parser, but it is not the v1 primary parser because it does not natively provide the page, media, OCR, coordinate, and artifact structure File2Doc requires.
