# File2Doc

File2Doc is a file parsing service that turns uploaded files into agent-friendly result packages.

The v1 direction is documented in:

- `CONTEXT.md`
- `docs/parse-result-v1.md`
- `docs/implementation-plan.md`
- `references/api-examples.md`
- `SKILL.md`
- `docs/adr/`

## Development

```bash
python3 -m pip install -e '.[dev]' -i https://mirrors.aliyun.com/pypi/simple --trusted-host mirrors.aliyun.com
python3 -m pytest -q
```

Run the service locally with auth disabled:

```bash
FILE2DOC_AUTH_ENABLED=false FILE2DOC_STORAGE_ROOT=/tmp/file2doc uvicorn file2doc.main:app --reload
```
