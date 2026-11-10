#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版校验：metadata.yaml / CHANGELOG.md / README.md / main.py 四处版本号必须一致。

还检查仓库自己定的版本号规则（见 README「版本号规则」）：
- 尾号取偶数 0/2/4/6/8；
- 版本号里不出现数字 1 和 7。

用法：
    python tools/check_version.py
退出码 0 = 通过，1 = 有问题（会打印原因）。
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    path = os.path.join(ROOT, name)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        print("缺少文件: %s" % name)
        return ""


def metadata_version():
    text = read("metadata.yaml")
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", text, re.M)
    return match.group(1) if match else ""


def changelog_version():
    text = read("CHANGELOG.md")
    match = re.search(r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    return match.group(1) if match else ""


def readme_version():
    text = read("README.md")
    match = re.search(r"^##\s*([0-9]+\.[0-9]+\.[0-9]+)", text, re.M)
    return match.group(1) if match else ""


def code_version():
    text = read("main.py")
    match = re.search(r'PLUGIN_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text)
    return match.group(1) if match else ""


def check_rules(version):
    problems = []
    parts = version.split(".")
    if len(parts) != 3:
        return ["版本号应该是 x.y.z"]
    tail = int(parts[2])
    if tail % 2 != 0:
        problems.append("尾号 %d 是奇数，按规则只能取 0/2/4/6/8" % tail)
    if "1" in version or "7" in version:
        problems.append("版本号 %s 里出现了数字 1 或 7，按规则要避开" % version)
    return problems


REPO_OWNER = "GM888888456"


def repo_problem():
    """仓库地址只认主仓库的规范写法：https://github.com/<owner>/<repo>/"""
    text = read("metadata.yaml")
    match = re.search(r"^repo:\s*(\S+)\s*$", text, re.M)
    if not match:
        return "metadata.yaml 缺少 repo 字段"
    url = match.group(1)
    pattern = r"^https://github\.com/%s/([A-Za-z0-9_.-]+)/$" % REPO_OWNER
    parsed = re.match(pattern, url)
    if not parsed:
        return ("repo 必须写成 https://github.com/%s/<仓库名>/（结尾带斜杠、指向主仓库），现在是 %s"
                % (REPO_OWNER, url))
    name = parsed.group(1)
    if name.endswith("_ui") or name.lower().endswith("_test"):
        return "repo 指向了测试仓库 %s，必须指向主仓库" % name
    return ""


def main():
    versions = {
        "metadata.yaml": metadata_version(),
        "CHANGELOG.md": changelog_version(),
        "README.md": readme_version(),
        "main.py": code_version(),
    }
    missing = [name for name, value in versions.items() if not value]
    if missing:
        print("读不到版本号: %s" % "、".join(missing))
        return 1
    distinct = set(versions.values())
    if len(distinct) != 1:
        print("版本号不一致：")
        for name, value in versions.items():
            print("  %-16s %s" % (name, value))
        return 1
    version = distinct.pop()
    problems = check_rules(version)
    if problems:
        print("版本号 %s 不符合仓库规则：" % version)
        for item in problems:
            print("  - " + item)
        return 1
    repo_issue = repo_problem()
    if repo_issue:
        print(repo_issue)
        return 1
    print("版本号校验通过：%s" % version)
    print("仓库地址规范：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
