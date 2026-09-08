"""面单 SKU 识别与排序核心逻辑。

三级识别，逐级兜底:
  text -> 直接读 PDF 文本层
  ocr  -> 整页渲染后本地 OCR (rapidocr)
  llm  -> 多模态大模型识别整页图片（Anthropic 或 OpenAI 协议端点）
"""

import base64
import concurrent.futures as cf
import json
import re
import threading

import fitz  # PyMuPDF

DEFAULT_PREFIXES = ["FDC", "HD", "BDL", "PQ", "AMB", "LZ"]
DEFAULT_THREADS = 4
MAX_THREADS = 16
# 复查时把页面切成几条 —— 3 条在放大倍数和调用次数之间比较平衡
RECHECK_BANDS = 3
# 连续这么多页失败就中止：多半是 Key/地址/模型配错，没必要把整批跑完
FAIL_STREAK_LIMIT = 5

# 部分中转站的 WAF 把 openai SDK 自带的 "OpenAI/Python x.y.z" 列入黑名单，
# 直接返回 Cloudflare 1010（Your request was blocked）。换成本工具自己的
# 标识即可正常通过，对官方 API 无影响。
USER_AGENT = "label-sorter/1.0"


def build_pattern(prefixes):
    """SKU 形如 HD-733-1*1 / FDC-260-1*2 / PQ-1855*4，数量后缀可省略。"""
    alt = "|".join(re.escape(p) for p in prefixes)
    return re.compile(
        r"(?:%s)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\s*\*\s*\d+)?" % alt
    )


def clean(tok):
    return re.sub(r"\s+", "", tok)


def natural_key(sku):
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


def extract_text_skus(page, pat):
    return dedup(pat.findall(page.get_text()))


class OcrEngine:
    """本地 OCR：整页渲染后识别，不依赖面单模板。"""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()

    def extract(self, page, pat, clip=None):
        import numpy as np
        pix = page.get_pixmap(matrix=fitz.Matrix(4, 4), clip=clip)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        result, _ = self._ocr(img)
        if not result:
            return []
        text = " ".join(line[1] for line in result)
        return dedup(pat.findall(text))


def page_png_b64(page, zoom=3, clip=None):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
    return base64.standard_b64encode(pix.tobytes("png")).decode()


def band_clips(page, bands=RECHECK_BANDS, overlap=0.18):
    """把页面横向切成带重叠的条带。

    多模态模型会把整页降采样到短边 ~768px，SKU 数字只剩几十像素；
    切成条带后同样的像素预算只覆盖三分之一页面，等效放大重读。
    条带留重叠，避免 SKU 正好压在切缝上被截断。
    """
    r = page.rect
    h = r.height
    out = []
    for i in range(bands):
        top = max(r.y0, r.y0 + h * (i / bands - overlap / 2))
        bot = min(r.y1, r.y0 + h * ((i + 1) / bands + overlap / 2))
        out.append(fitz.Rect(r.x0, top, r.x1, bot))
    return out


def recheck_page(page, pat, engine):
    """切条带重读一页，返回合并去重后的 SKU 列表。"""
    found = []
    for clip in band_clips(page):
        try:
            found += engine.extract(page, pat, clip=clip)
        except Exception:  # noqa: BLE001 — 复查失败就当没查到，不影响原结果
            pass
    return dedup(found)


def build_prompt(prefixes):
    return (
        "这是一张快递面单。找出面单上印刷的商品SKU编号，"
        f"SKU以 {'/'.join(prefixes)} 开头，"
        "形如 HD-733-1*1、FDC-260-1*2（*后为数量）。"
        "数字部分请逐位读准，连续重复的数字（如 722、1155）容易漏读一位，"
        "务必核对位数。"
        "按出现顺序返回全部SKU；没有则返回空列表。"
    )


def parse_skus(text, pat):
    """宽容解析模型回复：优先按 JSON 读，读不出就直接正则扫全文。

    第三方 OpenAI 兼容端点未必支持 JSON 模式，回复里常混着解释文字或
    ```json 围栏，所以两条路都留着。最后统一用正则全匹配校验，
    防止幻觉产物混入排序。
    """
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    cands = []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            cands = data.get("skus") or []
        elif isinstance(data, list):
            cands = data
    except json.JSONDecodeError:
        pass
    if not cands:
        cands = pat.findall(text)
    return dedup([s for s in cands if pat.fullmatch(clean(str(s)))])


class LlmEngine:
    """Claude 多模态识别：整页图片发给模型，结构化返回 SKU 列表。"""

    def __init__(self, prefixes, model="claude-opus-5", api_key=None):
        import anthropic
        kwargs = {"api_key": api_key} if api_key else {}
        self._client = anthropic.Anthropic(**kwargs)
        self._model = model
        self._prefixes = prefixes

    def extract(self, page, pat, clip=None):
        img_b64 = page_png_b64(page, clip=clip)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,  # 读面单是确定性任务，别让模型自由发挥
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
                    {"type": "text", "text": build_prompt(self._prefixes)},
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
        return parse_skus(text, pat)


class OpenAiEngine:
    """OpenAI 协议识别：base_url / api_key / model 全部可配。

    可对接 OpenAI 官方、各类中转站，以及 vLLM、Ollama、LM Studio 等
    自建的兼容端点。模型需支持图片输入。
    """

    def __init__(self, prefixes, model="gpt-4o", api_key=None, base_url=None):
        import openai
        kwargs = {"default_headers": {"User-Agent": USER_AGENT}}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url.rstrip("/")
        self._client = openai.OpenAI(**kwargs)
        self._model = model
        self._prefixes = prefixes
        # 可选参数，被端点拒绝后就地剥掉，对后续页面持续生效
        self._extra = {
            "max_tokens": 1024,
            "temperature": 0,  # 读面单是确定性任务，别让模型自由发挥
            "response_format": {"type": "json_object"},
        }

    def extract(self, page, pat, clip=None):
        content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:image/png;base64," + page_png_b64(page, clip=clip)
                },
            },
            {
                "type": "text",
                "text": build_prompt(self._prefixes) +
                '只输出 JSON，格式为 {"skus": ["HD-733-1*1"]}。',
            },
        ]
        return parse_skus(self._chat(content), pat)

    def _chat(self, content):
        # 兼容端点对可选参数支持不一，被拒就剥掉重试
        for _ in range(3):
            try:
                r = self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "user", "content": content}],
                    **self._extra,
                )
                return r.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 — 判不出就翻成可排查的说明抛出
                if not self._drop_unsupported(self._extra, str(e)):
                    raise RuntimeError(self._explain(e)) from e
        raise RuntimeError("接口连续拒绝请求参数，请检查 base_url 与模型名")

    def _explain(self, e):
        """把 SDK 异常翻成能照着排查的说明，重点是暴露真实请求地址。"""
        import openai
        if isinstance(e, openai.APIStatusError):
            url = str(getattr(e.response, "url", "") or "")
            body = (e.response.text or "")[:200].replace("\n", " ")
            tips = {
                401: "API Key 无效或未被接受",
                403: "被对方网关拒绝；常见于 Base URL 少了 /v1，"
                     "或中转站的 WAF 拦了本次请求",
                404: "地址或模型名不存在；确认 Base URL 填到 /v1 为止",
                429: "限流或余额不足",
            }
            tip = tips.get(e.status_code, "")
            if not tip and e.status_code >= 500:
                tip = "对方服务异常或地址不可达"
            return (f"接口返回 HTTP {e.status_code}"
                    f"{'：' + tip if tip else ''}\n"
                    f"  实际请求地址: {url}\n"
                    f"  对方响应: {body}")
        if isinstance(e, openai.APIConnectionError):
            return f"连不上 {self._client.base_url}，检查地址是否可达"
        return f"{type(e).__name__}: {e}"

    @staticmethod
    def _drop_unsupported(extra, err):
        """按报错剥掉端点不认的参数，返回本次是否有改动。"""
        low = err.lower()
        if "response_format" in low and "response_format" in extra:
            del extra["response_format"]
            return True
        if "max_tokens" in low and "max_tokens" in extra:
            # 新版模型改叫 max_completion_tokens
            extra["max_completion_tokens"] = extra.pop("max_tokens")
            return True
        if "max_completion_tokens" in low and "max_completion_tokens" in extra:
            del extra["max_completion_tokens"]
            return True
        if "temperature" in low and "temperature" in extra:
            # 部分新模型只接受默认温度
            del extra["temperature"]
            return True
        return False


def make_llm_engine(prefixes, provider="anthropic", model=None,
                    api_key=None, base_url=None):
    if provider == "openai":
        return OpenAiEngine(prefixes, model or "gpt-4o", api_key, base_url)
    return LlmEngine(prefixes, model or "claude-opus-5", api_key)


def missing_one_char(a, b):
    """a 是否等于 b 删掉一个字符，如 HD-72-3*1 之于 HD-722-3*1。"""
    if len(a) + 1 != len(b):
        return False
    i = 0
    for ch in b:
        if i < len(a) and a[i] == ch:
            i += 1
    return i == len(a)


def find_suspicious(counts, min_ratio=3):
    """挑出疑似漏读一位的 SKU：比某个高频 SKU 少一个字符，自己又很罕见。

    只提示不改数据 —— 两个 SKU 真实并存也完全可能，交给人判断。
    """
    out = []
    for a, na in counts.items():
        for b, nb in counts.items():
            if a != b and nb >= na * min_ratio and missing_one_char(a, b):
                out.append({"sku": a, "count": na, "like": b, "like_count": nb})
                break
    return sorted(out, key=lambda x: -x["like_count"])


def sort_and_merge(input_paths, output_path, prefixes=None,
                   fallback="ocr", model="claude-opus-5", api_key=None,
                   progress=None, provider="anthropic", base_url=None,
                   threads=DEFAULT_THREADS, recheck=True):
    """将多个 PDF 的全部页面按 SKU 全局排序后合并输出。

    provider: "anthropic" 走 Claude 官方接口；"openai" 走 OpenAI 协议，
              配合 base_url 可指向任意兼容端点。
    threads:  并发识别的线程数。识别以网络等待为主，并发能显著缩短总时长；
              OCR 走锁串行，因为它本身已经吃满多核。
    recheck:  对疑似漏读一位的页切条带重读一遍（见 recheck_page）。只针对
              少数可疑页，每页多 3 次调用。
    progress: 可选回调 progress(done, total, info_dict)，info_dict 含
              index/file/page/skus/source/error 字段。完成顺序与页序无关。
    返回统计 dict。
    """
    prefixes = prefixes or DEFAULT_PREFIXES
    pat = build_pattern(prefixes)
    threads = max(1, min(int(threads), MAX_THREADS))

    ocr = OcrEngine() if "ocr" in fallback else None
    llm = (make_llm_engine(prefixes, provider, model, api_key, base_url)
           if "llm" in fallback else None)

    page_counts = []
    for p in input_paths:
        d = fitz.open(p)
        page_counts.append(len(d))
        d.close()
    tasks = [(di, pi) for di, n in enumerate(page_counts) for pi in range(n)]
    total = len(tasks)

    # fitz.Document 不是线程安全的，每个线程独立打开自己那份
    local = threading.local()
    opened = []
    opened_lock = threading.Lock()
    ocr_lock = threading.Lock()

    def get_doc(di):
        cache = getattr(local, "docs", None)
        if cache is None:
            cache = local.docs = {}
        if di not in cache:
            cache[di] = fitz.open(input_paths[di])
            with opened_lock:
                opened.append(cache[di])
        return cache[di]

    def work(idx, di, pi):
        if progress:  # 让界面能显示"这几页正在跑"
            progress(-1, total, {
                "index": idx, "phase": "start",
                "file": input_paths[di].rsplit("/", 1)[-1], "page": pi + 1,
            })
        try:
            page = get_doc(di)[pi]
            skus, source = extract_text_skus(page, pat), "text"
            if not skus and ocr:
                # rapidocr 实例非线程安全；它内部已多核并行，串行调用不亏
                with ocr_lock:
                    skus, source = ocr.extract(page, pat), "ocr"
            if not skus and llm:
                skus, source = llm.extract(page, pat), "llm"
            return idx, di, pi, skus, source, None
        except Exception as e:  # noqa: BLE001 — 单页失败不该拖垮整批
            return idx, di, pi, [], "error", f"{type(e).__name__}: {e}"

    records = [None] * total  # (doc_idx, page_idx, primary|None, skus, source)
    errors = []
    rechecked = []
    done = 0
    streak = 0  # 连续失败数，用于熔断
    try:
        with cf.ThreadPoolExecutor(max_workers=threads) as pool:
            futs = [pool.submit(work, i, di, pi)
                    for i, (di, pi) in enumerate(tasks)]
            for fut in cf.as_completed(futs):
                idx, di, pi, skus, source, err = fut.result()
                primary = skus[0] if skus else None
                records[idx] = (di, pi, primary, skus, source)
                done += 1
                name = input_paths[di].rsplit("/", 1)[-1]
                if err:
                    errors.append({"file": name, "page": pi + 1, "error": err})
                    streak += 1
                else:
                    streak = 0
                # 先把这页的状态推出去，再决定是否熔断，否则界面上它会卡在"处理中"
                if progress:
                    progress(done, total, {
                        "index": idx,
                        "file": name,
                        "page": pi + 1,
                        "skus": skus,
                        "source": source if skus else ("error" if err else "none"),
                        "error": err,
                    })
                if streak >= FAIL_STREAK_LIMIT:
                    for f in futs:
                        f.cancel()
                    raise RuntimeError(
                        f"连续 {FAIL_STREAK_LIMIT} 页识别失败，已中止。"
                        f"最后一条错误：\n{err}")
        # ---- 复查：对疑似漏读一位的页切条带重读 ----
        engine = llm or ocr
        if engine and recheck:
            counts0 = {}
            for r in records:
                if r[2]:
                    counts0[r[2]] = counts0.get(r[2], 0) + 1
            bad = {s["sku"] for s in find_suspicious(counts0)}
            # 文本层读出来的是准的，只复查模型/OCR 读的页
            todo = [i for i, r in enumerate(records)
                    if r[2] in bad and r[4] in ("llm", "ocr")]

            def redo(idx):
                di, pi, primary, _, _ = records[idx]
                page = get_doc(di)[pi]
                if engine is ocr:  # 同样非线程安全，串行走
                    with ocr_lock:
                        return idx, primary, recheck_page(page, pat, engine)
                return idx, primary, recheck_page(page, pat, engine)

            if todo:
                with cf.ThreadPoolExecutor(max_workers=threads) as pool:
                    for idx, before, skus in pool.map(redo, todo):
                        di, pi, _, _, source = records[idx]
                        after = skus[0] if skus else None
                        changed = bool(after) and after != before
                        if changed:  # 切块放大后读到的更可信，采用它
                            records[idx] = (di, pi, after, skus, source)
                        rechecked.append({
                            "file": input_paths[di].rsplit("/", 1)[-1],
                            "page": pi + 1, "before": before,
                            "after": after, "changed": changed,
                        })
                        if progress:
                            progress(done, total, {
                                "index": idx, "phase": "recheck",
                                "file": input_paths[di].rsplit("/", 1)[-1],
                                "page": pi + 1,
                                "skus": records[idx][3],
                                "source": source,
                                "before": before, "changed": changed,
                                "error": None,
                            })
    finally:
        for d in opened:
            d.close()

    # 稳定排序：有SKU的按自然序，无SKU的保持原顺序排在末尾
    order = sorted(range(total), key=lambda i: (
        (1, ()) if records[i][2] is None else (0, natural_key(records[i][2]))
    ))

    docs = [fitz.open(p) for p in input_paths]
    out = fitz.open()
    remaining = page_counts.copy()
    try:
        for i in order:
            di, pi, *_ = records[i]
            remaining[di] -= 1
            # Keep each source document's graft map until its final page. Without
            # this, shared fonts and images are copied again for every page.
            out.insert_pdf(
                docs[di], from_page=pi, to_page=pi,
                final=remaining[di] == 0,
            )
        # Compact unused objects without costly cross-object deduplication.
        # Shared source resources are already preserved by the graft maps above.
        out.save(output_path, garbage=2, deflate=True, use_objstms=1)
    finally:
        out.close()
        for d in docs:
            d.close()

    counts = {}
    sku_counts = {}
    unrecognized = []
    for di, pi, primary, _, source in records:
        if primary:
            counts[source] = counts.get(source, 0) + 1
            sku_counts[primary] = sku_counts.get(primary, 0) + 1
            continue
        # 出错和"读完了但没找到SKU"要分开算，前者是故障后者是正常结果
        key = "error" if source == "error" else "none"
        counts[key] = counts.get(key, 0) + 1
        if key == "none":
            unrecognized.append(
                {"file": input_paths[di].rsplit("/", 1)[-1], "page": pi + 1})

    suspicious = find_suspicious(sku_counts)
    for s in suspicious:  # 附上具体页码，方便人工核对
        s["pages"] = [{"file": input_paths[di].rsplit("/", 1)[-1], "page": pi + 1}
                      for di, pi, primary, _, _ in records if primary == s["sku"]]
    return {"total": total, "sources": counts, "unrecognized": unrecognized,
            "suspicious": suspicious, "errors": errors,
            "rechecked": rechecked}
