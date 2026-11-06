---
name: data-analysis
description: Use this skill when the user wants to analyze, summarize, clean, or visualize tabular data (CSV, TSV, Excel) and the answer is numbers, findings, or charts rather than a spreadsheet to edit. Typical asks - "分析一下这份销售数据", "按月汇总", "画个趋势图", "找出异常值", "哪些客户占了大部分收入". Also use first when a CSV/TXT fails to open or shows garbled Chinese (encoding problem). If the deliverable itself is an .xlsx file with formulas or formatting, use the xlsx skill instead; this skill can be combined with it.
---

# Data analysis

Environment: no network, pandas / numpy / matplotlib / seaborn / plotly / charset_normalizer are preinstalled. Inputs are in `/workspace/uploads` (read-only), write files to `/workspace/output`, scratch work to `/workspace/scratchpad`.

## 1. Load the file correctly

Chinese CSV/TXT files are often GBK, not UTF-8. A `UnicodeDecodeError` or garbled headers means the encoding is wrong. Detect it, do not guess:

```python
import charset_normalizer, pandas as pd

def read_table(path):
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return pd.read_excel(path)          # .xls needs xlrd, already installed
    raw = open(path, "rb").read()
    enc = charset_normalizer.from_bytes(raw).best().encoding
    if enc and enc.lower() in ("gb2312", "gbk"):
        enc = "gb18030"                     # superset, safe for all three
    return pd.read_csv(path, encoding=enc, sep=None, engine="python")  # sep=None sniffs , ; \t

df = read_table("/workspace/uploads/data.csv")
```

If the file has several sheets, list them first (`pd.ExcelFile(path).sheet_names`) and say which one you used. If the header is not on the first row, find the real header row before analyzing.

## 2. Look before you compute

Always print these first, and read them:

```python
print(df.shape)
print(df.dtypes)
print(df.head(10))
print(df.isna().sum())
print(df.describe(include="all").T)
print(df.duplicated().sum())
```

Check for: numbers stored as text (`"1,234"`, `"12%"`, `"¥100"`), dates stored as text, mixed units, totals or subtotal rows inside the data, duplicate rows, and obviously impossible values (negative quantities, dates in the future). Fix what is clearly a format problem. For anything that changes the meaning (dropping rows, filling missing values), do it only if it is clearly right, and tell the user what you did and how many rows were affected.

## 3. Compute, then sanity-check

- Do the aggregation in pandas, never by eyeballing `head()`.
- After a groupby, check that parts add up to the whole (`df["x"].sum()` vs the grouped total). After a merge, compare row counts before and after; a jump means a many-to-many join.
- Report a number together with its basis: "2024 年 Q3 销售额 128.4 万元(剔除 17 条退款记录后)".
- Percent of total, growth rate, and averages are easy to define two ways. State which one you used.
- Do not present a correlation as a cause, and do not draw conclusions from a handful of rows. Say when the sample is small.

## 4. Charts

Chinese text needs a CJK font, or it shows as empty squares. Set it once, and point the matplotlib cache at a writable directory (the home directory is read-only):

```python
import os
os.environ["MPLCONFIGDIR"] = "/tmp/mpl"
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC"]
plt.rcParams["axes.unicode_minus"] = False   # otherwise the minus sign becomes a square

fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
monthly.plot(ax=ax, marker="o")
ax.set_title("月度销售额")
ax.set_xlabel("月份")
ax.set_ylabel("销售额(元)")
fig.tight_layout()
fig.savefig("/workspace/output/monthly_sales.png")
plt.close(fig)
```

Choosing the chart: trend over time -> line; compare categories -> bar (sorted by value, horizontal if labels are long); share of a whole with few parts -> bar or stacked bar (avoid pie with more than 5 slices); distribution -> histogram or box; relation of two numbers -> scatter. One message per chart; label axes with units; no 3D.

After saving, **look at the picture** if `view_image` is available, to catch overlapping labels, squares instead of Chinese, and empty plots. If it is not available, at least check the file exists and is not tiny.

If the user wants an interactive chart inside the conversation instead of a file, build a self-contained HTML (data inlined, no external scripts or images, there is no network) and show it with `display_widget`.

## 5. Deliver

- Put charts and any cleaned data in `/workspace/output` and hand them over with `present_files`. Save cleaned data as UTF-8 with BOM (`encoding="utf-8-sig"`) if it is a CSV that the user will open in Excel; plain UTF-8 shows garbled Chinese there.
- If the user needs a formatted workbook, formulas, or a report document, switch to the `xlsx` / `docx` / `pptx` skill and reuse the numbers you already verified.
- In the reply, lead with the finding, then the evidence, then caveats (data problems you found, rows excluded, assumptions). Keep the numbers consistent between the reply, the charts, and the files.
