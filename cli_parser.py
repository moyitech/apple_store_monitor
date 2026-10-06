"""中文命令行帮助与参数错误提示。"""

import argparse
import re
import sys


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, "用法：" if prefix is None else prefix)


class ChineseRawDescriptionHelpFormatter(ChineseHelpFormatter, argparse.RawDescriptionHelpFormatter):
    pass


class ChineseArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", ChineseHelpFormatter)
        add_help = kwargs.pop("add_help", True)
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = "位置参数"
        self._optionals.title = "选项"
        if add_help:
            self.add_argument("-h", "--help", action="help", help="显示帮助信息并退出")

    def error(self, message):
        translations = [
            (r"the following arguments are required: (.*)", r"缺少必需参数：\1"),
            (r"unrecognized arguments: (.*)", r"无法识别的参数：\1"),
            (r"invalid choice: (.*?) \(choose from (.*?)\)", r"无效选项：\1（可选值：\2）"),
            (r"expected one argument", "需要提供一个参数值"),
            (r"expected at least one argument", "需要提供至少一个参数值"),
            (r"argument (.*?): ", r"参数 \1："),
        ]
        for pattern, replacement in translations:
            message = re.sub(pattern, replacement, message)
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：错误：{message}\n")
