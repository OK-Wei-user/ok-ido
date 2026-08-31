---
name: summarize
description: Summarize URLs, local files, and content using built-in tools. Use when users request summaries, abstracts, key points extraction, or content condensation.
enabled: true
keywords: [summarize, summary, 摘要, 总结, 概括, 提炼, abstract, tldr, key points]
file_extensions: []
---

# Summarize

Summarize URLs, local files, and content using the agent's built-in capabilities (Python + LLM), without requiring any external CLI tools.

## Core Principle

**You are an LLM agent — you can summarize content directly.** This skill guides you on how to extract content from various sources and then summarize it yourself.

## Workflow

### Step 1: Extract content from source

Choose the appropriate extraction method based on the source type:

#### Web URLs
```bash
# Use curl to fetch HTML, then extract text with Python
curl -sL "https://example.com" | python3 -c "
import sys
from html.parser import HTMLParser

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.skip = False
    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav', 'footer', 'header'):
            self.skip = True
    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav', 'footer', 'header'):
            self.skip = False
    def handle_data(self, data):
        if not self.skip:
            text = data.strip()
            if text:
                self.text.append(text)

extractor = TextExtractor()
extractor.feed(sys.stdin.read())
print('\n'.join(extractor.text[:500]))
"
```

#### PDF files
```python
import pypdf
reader = pypdf.PdfReader("/path/to/file.pdf")
text = "\n".join(page.extract_text() or "" for page in reader.pages)
print(text[:5000])
```

#### Word documents (.docx)
```python
from markitdown import MarkItDown
md = MarkItDown()
result = md.convert("/path/to/file.docx")
print(result.text_content[:5000])
```

#### Excel spreadsheets (.xlsx, .xls)
```python
import pandas as pd
# .xls files may be HTML format — check header first
with open("/path/to/file.xls", "rb") as f:
    header = f.read(8)
if header.strip().startswith(b"<"):
    dfs = pd.read_html("/path/to/file.xls")
else:
    dfs = [pd.read_excel("/path/to/file.xls")]
for df in dfs:
    print(df.to_string()[:3000])
```

#### Plain text / Markdown files
```bash
cat "/path/to/file.md" | head -200
```

### Step 2: Summarize the extracted content

After extracting the content, use your own LLM capabilities to:

1. **Identify key themes** — What are the main topics?
2. **Extract key points** — What are the essential takeaways?
3. **Condense** — Rewrite in a concise format
4. **Structure** — Use bullet points, numbered lists, or sections as appropriate

### Summary length options

- **Short** (~100 words): Core message only
- **Medium** (~300 words): Key points with brief context
- **Long** (~500 words): Detailed summary with examples
- **Detailed**: Comprehensive summary preserving important details

### Best practices

- For long documents, extract content in chunks and summarize progressively
- For structured data (tables, lists), preserve the structure in the summary
- For multi-page PDFs, summarize each section then combine
- Always cite the source and mention the scope of the summary
- If the content is in Chinese, summarize in Chinese; if in English, summarize in English

## Example: Summarize a web article

```bash
# Step 1: Extract
content=$(curl -sL "https://example.com/article" | python3 -c "
import sys, re
html = sys.stdin.read()
text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
text = re.sub(r'<[^>]+>', ' ', text)
text = re.sub(r'\s+', ' ', text).strip()
print(text[:8000])
")

# Step 2: Summarize (you do this directly as the LLM agent)
# Analyze the extracted content and produce a structured summary
```

## Example: Summarize a PDF report

```python
import pypdf

reader = pypdf.PdfReader("/home/ubuntu/upload/report.pdf")
full_text = ""
for i, page in enumerate(reader.pages):
    text = page.extract_text() or ""
    full_text += f"\n--- Page {i+1} ---\n{text}"
    if len(full_text) > 20000:
        full_text = full_text[:20000] + "\n...[truncated]"
        break

print(full_text)
# Then summarize the extracted text directly
```
