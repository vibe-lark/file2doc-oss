# Third-party notice

`file2doc-markitdown-visual` is an independently maintained derivative informed
by Microsoft's `markitdown-ocr` plugin and MarkItDown Core image converter at
the exact source commit reviewed on 2026-07-16:

- Commit: `e144e0a2be95b34df17433bac904e635f2c5e551`
- https://github.com/microsoft/markitdown/tree/e144e0a2be95b34df17433bac904e635f2c5e551/packages/markitdown-ocr
- https://github.com/microsoft/markitdown/blob/e144e0a2be95b34df17433bac904e635f2c5e551/packages/markitdown/src/markitdown/converters/_image_converter.py

The derivative preserves the standard `markitdown.plugin` registration model,
converter priority, generic detailed-image-description intent, supported image
metadata fields, and inline Markdown boundary. Provider invocation, structured
result validation, and failure semantics are independently implemented for
File2Doc and do not promise future upstream compatibility.

This package is independently versioned even though it is currently shipped
inside the File2Doc wheel. `PACKAGE-METADATA.json` is the version source and
provenance manifest; `sbom.cdx.json` records the embedded package, exact
MarkItDown Core pin, and derivative source for release auditing.

The relevant upstream code is licensed under the MIT License:

> Copyright (c) Microsoft Corporation.
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.
