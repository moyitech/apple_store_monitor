"""配置向导的中文交互提示；不修改 questionary 的全局设置或依赖文件。"""

import re
from functools import partial

import questionary
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.layout.controls import FormattedTextControl


def _translate_tokens(tokens, kind):
    merged = []
    for token in tokens:
        # 单行提示在 prompt_toolkit 中可能已拆成逐字片段，先合并答案文本。
        if (merged and "class:answer" in token[0]
                and token[0] == merged[-1][0] and len(token) == len(merged[-1]) == 2):
            merged[-1] = (token[0], merged[-1][1] + token[1])
        else:
            merged.append(token)
    translated = []
    for style, value, *extra in merged:
        if "class:answer" in style:
            if kind == "confirm":
                value = {"Yes": "是", "No": "否"}.get(value, value)
            elif kind == "checkbox":
                value = "未选择任何项" if value == "done" else re.sub(
                    r"^done \((\d+) selections\)$", r"已选择 \1 项", value)
        translated.append((style, value, *extra))
    return translated


def _localize(question, kind):
    # questionary 没有多选结果、确认结果的语言选项，按语义样式翻译渲染文本。
    seen = set()
    for control in question.application.layout.find_all_controls():
        if id(control) in seen:
            continue
        seen.add(id(control))
        if not isinstance(control, FormattedTextControl):
            continue
        original = control.text

        def translated(original=original):
            return _translate_tokens(to_formatted_text(original), kind)

        control.text = translated
    for window in question.application.layout.find_all_windows():
        original_prefix = window.get_line_prefix
        if original_prefix is not None:
            def translated_prefix(line_number, wrap_count, original=original_prefix):
                return _translate_tokens(to_formatted_text(original(line_number, wrap_count)), kind)
            window.get_line_prefix = translated_prefix
    question.ask = partial(question.ask, kbi_msg="已取消当前操作。")
    return question


def select(message, **kwargs):
    searching = bool(kwargs.get("use_search_filter"))
    kwargs.setdefault("instruction", "（方向键移动，输入文字过滤，回车确认）" if searching
                      else "（方向键移动，回车确认）")
    if searching:
        kwargs.setdefault("use_jk_keys", False)
    return _localize(questionary.select(message, **kwargs), "select")


def checkbox(message, **kwargs):
    searching = bool(kwargs.get("use_search_filter"))
    kwargs.setdefault("instruction", "（方向键移动，空格勾选，输入文字过滤，回车确认）" if searching
                      else "（方向键移动，空格勾选，a 全选或取消全选，i 反选，回车确认）")
    if searching:
        kwargs.setdefault("use_jk_keys", False)
    return _localize(questionary.checkbox(message, **kwargs), "checkbox")


def confirm(message, default=True, **kwargs):
    kwargs.setdefault("instruction", f"（y 表示是，n 表示否，回车默认：{'是' if default else '否'}）")
    return _localize(questionary.confirm(message, default=default, **kwargs), "confirm")


def text(message, **kwargs):
    return _localize(questionary.text(message, **kwargs), "text")
