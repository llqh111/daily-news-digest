"""单篇正文解析进程；输入 HTML，标准输出仅返回正文。"""

import sys

import trafilatura


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    text = trafilatura.extract(
        sys.stdin.read(), include_comments=False,
        include_tables=False, no_fallback=False,
    )
    sys.stdout.write(text or "")
