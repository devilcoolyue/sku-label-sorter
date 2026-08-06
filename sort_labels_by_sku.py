#!/usr/bin/env python3
"""按面单上的 SKU 对 PDF 页面排序，生成新 PDF。

流程（三级识别，逐级兜底）:
  1. text  — 直接读 PDF 文本层的 SKU（最快，零成本）
  2. ocr   — 文本层没有 SKU 时，整页渲染后用本地 OCR (rapidocr) 识别
  3. llm   — 可选：用 Claude 多模态接口识别整页图片（--fallback llm，
             或 --fallback ocr+llm 表示 OCR 也没认出来时再交给大模型）

用法:
  python sort_labels_by_sku.py input.pdf -o sorted.pdf
  python sort_labels_by_sku.py input.pdf -o sorted.pdf --prefixes FDC,HD,BDL,PQ,AMB,LZ
  python sort_labels_by_sku.py input.pdf -o sorted.pdf --fallback llm          # 图片SKU全部走大模型
  python sort_labels_by_sku.py input.pdf -o sorted.pdf --fallback ocr+llm     # OCR兜底,认不出再走大模型
  python sort_labels_by_sku.py input.pdf -o sorted.pdf --fallback none        # 只用文本层

依赖:
  pip install pymupdf                 # 必装
  pip install rapidocr-onnxruntime opencv-python-headless   # --fallback 含 ocr 时
  pip install anthropic               # --fallback 含 llm 时（需 ANTHROPIC_API_KEY 或 ant auth login）
"""

import argparse
import base64
import json
import re
import sys

import fitz  # PyMuPDF

DEFAULT_PREFIXES = "FDC,HD,BDL,PQ,AMB,LZ"


def build_pattern(prefixes: list[str]) -> re.Pattern:
    """SKU 形如 HD-733-1*1 / FDC-260-1*2 / PQ-1855*4，数量后缀可省略。"""
    alt = "|".join(re.escape(p) for p in prefixes)
    return re.compile(
        r"(?:%s)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\s*\*\s*\d+)?" % alt
    )


def clean(tok: str) -> str:
    return re.sub(r"\s+", "", tok)


def natural_key(sku: str):
    """自然排序: HD-2 排在 HD-10 前面。"""
    parts = re.split(r"(\d+)", sku)
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def dedup(tokens):
    seen = []
    for t in tokens:
        c = clean(t)
        if c not in seen:
            seen.append(c)
    return seen


def extract_text_skus(page: fitz.Page, pat: re.Pattern) -> list[str]:
    return dedup(pat.findall(page.get_text()))


class OcrEngine:
    """本地 OCR：整页渲染后识别，不依赖面单模板。"""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()

    def extract(self, page: fitz.Page, pat: re.Pattern) -> list[str]:
        import numpy as np
        pix = page.get_pixmap(matrix=fitz.Matrix(4, 4))
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        result, _ = self._ocr(img)
        if not result:
            return []
        text = " ".join(line[1] for line in result)
        return dedup(pat.findall(text))


class LlmEngine:
    """Claude 多模态识别：把整页图片发给模型，结构化返回 SKU 列表。"""

    def __init__(self, prefixes: list[str], model: str = "claude-opus-5"):
        import anthropic
        self._client = anthropic.Anthropic()
        self._model = model
        self._prefixes = prefixes

    def extract(self, page: fitz.Page, pat: re.Pattern) -> list[str]:
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        img_b64 = base64.standard_b64encode(pix.tobytes("png")).decode()

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "这是一张快递面单。找出面单上印刷的商品SKU编号，"
                            f"SKU以 {'/'.join(self._prefixes)} 开头，"
                            "形如 HD-733-1*1、FDC-260-1*2（*后为数量）。"
                            "按出现顺序返回全部SKU；没有则返回空列表。"
                        ),
                    },
                ],
            }],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "skus": {"type": "array", "items": {"type": "string"}}
                        },
                        "required": ["skus"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        if response.stop_reason == "refusal":
            return []
        text = next(b.text for b in response.content if b.type == "text")
        skus = json.loads(text)["skus"]
        # 用同一正则校验模型输出，防止幻觉产物混入排序
        return dedup([s for s in skus if pat.fullmatch(clean(s))])


def main():
    ap = argparse.ArgumentParser(description="按面单SKU排序PDF")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--prefixes", default=DEFAULT_PREFIXES,
                    help=f"SKU前缀，逗号分隔（默认 {DEFAULT_PREFIXES}）")
    ap.add_argument("--fallback", default="ocr",
                    choices=["none", "ocr", "llm", "ocr+llm"],
                    help="文本层无SKU时的兜底方案（默认 ocr）")
    ap.add_argument("--model", default="claude-opus-5",
                    help="llm 兜底使用的 Claude 模型")
    args = ap.parse_args()

    prefixes = [p.strip().upper() for p in args.prefixes.split(",") if p.strip()]
    pat = build_pattern(prefixes)

    ocr = OcrEngine() if "ocr" in args.fallback else None
    llm = LlmEngine(prefixes, args.model) if "llm" in args.fallback else None

    doc = fitz.open(args.input)
    n = len(doc)
    print(f"共 {n} 页", flush=True)

    records = []  # (page_index, primary_sku | None, source)
    for i in range(n):
        page = doc[i]
        skus, source = extract_text_skus(page, pat), "text"
        if not skus and ocr:
            skus, source = ocr.extract(page, pat), "ocr"
        if not skus and llm:
            skus, source = llm.extract(page, pat), "llm"
        primary = skus[0] if skus else None
        records.append((i, primary, source))
        if source != "text" or not skus:
            print(f"  第{i + 1}页 [{source}] -> {skus or '未识别'}", flush=True)

    missing = [i + 1 for i, p, _ in records if p is None]
    if missing:
        print(f"警告: {len(missing)} 页未识别到SKU，将排在末尾: {missing}",
              file=sys.stderr)

    # 稳定排序：有SKU的按自然序，无SKU的保持原顺序排在末尾
    order = sorted(range(n), key=lambda i: (
        (1, ()) if records[i][1] is None else (0, natural_key(records[i][1]))
    ))

    doc.select(order)
    doc.save(args.output)
    counts = {}
    for _, _, src in records:
        counts[src] = counts.get(src, 0) + 1
    print(f"完成: {args.output}  (识别来源统计: {counts})")


if __name__ == "__main__":
    main()
