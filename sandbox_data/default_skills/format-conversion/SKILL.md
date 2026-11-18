---
name: format-conversion
description: Use this skill when the user wants to convert a file from one format to another, or to open an old-format file (.doc, .xls, .ppt) so it can be read or edited. Typical asks - "把 doc 转成 docx", "xls 转 xlsx", "Markdown 转 Word", "Word/PPT/Excel 转 PDF", "PDF 转 Word". It says which route keeps the most fidelity and which routes lose layout, so the user can be warned before they rely on the result. For editing the content of a converted file, continue with the docx / xlsx / pptx / pdf skill.
---

# Format conversion

Everything here runs offline in the sandbox with LibreOffice and pandoc. Inputs in `/workspace/uploads` are read-only, so write results to `/workspace/output` (use `--outdir`) and hand them over with `present_files`.

## LibreOffice helper

Always go through the helper, it configures LibreOffice for the sandbox:

```bash
python /workspace/skills/docx/scripts/office/soffice.py --headless \
  --convert-to <target> --outdir /workspace/output /workspace/uploads/<file>
```

If LibreOffice fails with a profile or permission error, the home directory is read-only; run `export HOME=/tmp/lo` first. The first run is slow (profile setup); give it time and do not start a second copy in parallel.

## Which route, and how reliable it is

| From -> To | Command / tool | Fidelity |
|---|---|---|
| .doc -> .docx | `--convert-to docx` | Good. Text and structure kept; complex layout may shift slightly |
| .xls -> .xlsx | `--convert-to xlsx` | Good. Values and sheets kept; check formulas and number formats |
| .ppt -> .pptx | `--convert-to pptx` | Text kept; check fonts, animations, and embedded objects |
| .docx/.pptx/.xlsx -> .pdf | `--convert-to pdf` | Good, and the usual way to produce a fixed-layout copy |
| .docx -> .doc / .xlsx -> .xls / .pptx -> .ppt | `--convert-to doc` / `xls` / `ppt` | Works; modern-only features are dropped. Only do this if the user needs the old format |
| Markdown -> .docx | `pandoc in.md -o out.docx` | Good for headings, lists, tables, bold. Plain styling; use the docx skill if it must look designed |
| .docx -> Markdown | `pandoc in.docx -t gfm -o out.md` | Good for text; images are lost unless you add `--extract-media=DIR` |
| .html -> .docx | `pandoc in.html -o out.docx` | Fair; CSS is ignored |
| .csv <-> .xlsx | pandas (see the data-analysis skill for GBK files) | Exact for values; CSV has no formatting or formulas |
| **PDF -> .docx** | see below | **Poor** |
| **PDF -> .pptx / .xlsx** | not supported reliably | Do not promise it |

## PDF to Word: be honest about it

LibreOffice cannot open a PDF as a Word document by default. Forcing it works:

```bash
python /workspace/skills/docx/scripts/office/soffice.py --headless \
  --infilter=writer_pdf_import --convert-to "docx:MS Word 2007 XML" \
  --outdir /workspace/output /workspace/uploads/in.pdf
```

but in testing the result is a page of positioned text boxes, not reflowable paragraphs: the text is there, yet editing it in Word is awkward, and the layout can move. Choose by what the user actually needs:

- They need the **text** (to copy, quote, rewrite): extract it with the pdf skill (pdfplumber / pypdf, or OCR for scans) and build a clean .docx with the docx skill. This usually gives a better document than conversion.
- They need to **edit the original layout**: use the forced conversion, and tell them up front it is approximate and needs manual cleanup.
- The PDF is a **scan**: there is no text layer; OCR first (pdf skill, `lang="chi_sim+eng"`).

## Always verify the result

A conversion that exits with code 0 is not a good conversion.

1. The output file exists and is not empty.
2. Re-open it with the library for its type (python-docx, openpyxl / pandas, python-pptx) and compare against the source: paragraph / sheet / slide counts, and a few specific values.
3. For layout-sensitive files, render to PDF and look at a page or two (`pdftoppm -r 70 -png -f 1 -l 2`, then `view_image` if available).
4. Tell the user what changed or may have changed (fonts, formulas, animations, images). If you checked nothing beyond "it opened", say so.

Keep the original file name stem and change only the extension, so the user can tell which file is which.
