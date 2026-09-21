# Brand assets

## `paineasy-logo.png`

Drop the PainEasy logo here as **`paineasy-logo`** with any common image
extension — `.png`, `.jpg`, `.jpeg`, `.webp` or `.gif` — and every generated
invoice, receipt and insurance statement picks it up automatically. `logo.*` is
also accepted.

The PDF header falls back to a text wordmark when the file is absent, so a
missing logo degrades the look of a document rather than breaking generation —
worth knowing if a deployment forgets to copy it.

- Transparent or white background
- Any size. It is scaled to ~22 mm wide in the header, so ≥400 px wide keeps it
  crisp in print. Larger sources are **downscaled to 600 px and cached** before
  embedding: a 1000x1000 original added ~107 KB to every document, which is
  bandwidth wasted on pixels nobody can see once these are emailed.
- Replacing the file takes effect immediately; the cache is keyed on its
  modification time, so no restart is needed
- Brand colours used elsewhere in the documents:
  - navy `#123A6E` (the "PAIN" wordmark, headings, table headers)
  - blue `#1C7FC4` (the "EASY" wordmark, accents, rules)

Override the path with `CLINIC_LOGO_PATH` if the file lives elsewhere.
