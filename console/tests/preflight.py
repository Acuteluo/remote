#!/usr/bin/env python3
"""启动前静态预检 —— 专抓"重启才会炸"的那类问题。

为什么要有它
------------
2026-09-22 的事故: lan_forward.py 少了 `import os`。那个文件**语法完全合法**,
py_compile 判通过, 但模块级代码一执行就炸:

    HERE = os.path.dirname(os.path.abspath(__file__))
    NameError: name 'os' is not defined

正在跑的旧进程内存里揣着老代码, 毫发无损; 改坏的文件要等到**下次重启**才会第一次
真正被执行 —— 于是开机即崩溃循环(systemd Restart=on-failure 每 3 秒拉一次, 空转
255 次), 转发器不再镜像任何端口, 表现是远程桌面"拒绝连接"。而 systemd 那侧只看到
它在不停重启, 没有任何一处说"整体是坏的"。

py_compile 查不出"用了却没有导入的名字", 所以这里补两件事:

  1. 语法     —— compile() 每个文件
  2. 未定义名 —— AST 扫描: 收集整个文件里所有**被绑定**的名字(import / 赋值 /
     def / class / 形参 / with-as / except-as / for 目标 / 推导式 / global /
     nonlocal), 再报出"被读取、但到处都没绑定、也不是内置"的名字。
     缺 import 必然被抓; 有 `import *` 的文件直接跳过判定(无法静态确定)。

纯标准库、不需要任何服务在跑, 可以直接塞进 systemd 的 ExecStartPre。

用法:
    python3 console/tests/preflight.py              # 查运行时文件(默认)
    python3 console/tests/preflight.py --all        # 连 tests/ 一起查
    python3 console/tests/preflight.py --quiet      # 只在出错时输出
    python3 console/tests/preflight.py a.py b.py    # 只查指定文件
退出码: 0 = 通过, 1 = 有问题
"""
import ast
import builtins
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.dirname(HERE)
REPO = os.path.dirname(CONSOLE)

# 遍历时跳过的目录名(任意层级)
SKIP_DIRS = {"__pycache__", ".git", "artifacts", "vendor", "node_modules", "storage", "dev"}

# console/tests/ 默认只当开发工具, 但这两个是**运行时入口**(systemd 单元直接调),
# 所以无论如何都要一起预检
EXTRA_RUNTIME = (os.path.join(HERE, "preflight.py"), os.path.join(HERE, "stack_guard.py"))

# 运行时环境注入的模块级名字, 不是"未定义"
AMBIENT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__builtins__", "__debug__", "__cached__", "__path__", "WindowsError",
}
BUILTINS = set(dir(builtins)) | AMBIENT


def iter_py(include_tests=False):
    """默认只查运行时会被执行的文件; console/tests 是开发工具, 不算运行时。"""
    out = []
    for root, sub, files in os.walk(REPO):
        sub[:] = [d for d in sub if d not in SKIP_DIRS]
        if not include_tests and os.path.abspath(root).startswith(os.path.abspath(HERE)):
            continue
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return sorted(out)


class _Bindings(ast.NodeVisitor):
    """收集"被绑定"与"被读取"的名字。宁可不报, 不可误报。"""

    def __init__(self):
        self.bound = set()
        self.used = []           # [(name, lineno)]

    # ---- 绑定 ----
    def _args(self, a):
        for x in (list(getattr(a, "posonlyargs", [])) + list(a.args)
                  + list(a.kwonlyargs)):
            self.bound.add(x.arg)
        for x in (a.vararg, a.kwarg):
            if x is not None:
                self.bound.add(x.arg)

    def visit_Import(self, node):
        for al in node.names:
            self.bound.add((al.asname or al.name).split(".")[0])

    def visit_ImportFrom(self, node):
        for al in node.names:
            if al.name == "*":
                self.bound.add("*")      # 有星号导入: 放弃判定
            else:
                self.bound.add(al.asname or al.name)

    def visit_FunctionDef(self, node):
        self.bound.add(node.name)
        self._args(node.args)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._args(node.args)
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.bound.add(node.name)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
        self.generic_visit(node)

    def visit_Global(self, node):
        self.bound.update(node.names)

    def visit_Nonlocal(self, node):
        self.bound.update(node.names)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)
        else:
            self.used.append((node.id, node.lineno))
        self.generic_visit(node)


def check_file(path):
    """返回 (ok, 消息列表)。"""
    problems = []
    try:
        with open(path, "rb") as f:
            src = f.read()
    except OSError as e:
        return False, ["读不到: %s" % e]

    # 1. 语法
    try:
        compile(src, path, "exec", dont_inherit=True)
    except SyntaxError as e:
        return False, ["语法错误 第 %s 行: %s" % (e.lineno, e.msg)]

    # 2. 未定义名
    try:
        tree = ast.parse(src, path)
    except SyntaxError as e:                      # 上面已查过, 这里兜底
        return False, ["解析失败 第 %s 行: %s" % (e.lineno, e.msg)]

    b = _Bindings()
    b.visit(tree)
    if "*" in b.bound:
        return True, []                           # 有 import *, 放弃判定

    seen = set()
    for name, lineno in b.used:
        if name in b.bound or name in BUILTINS or name in seen:
            continue
        seen.add(name)
        problems.append("第 %d 行: 名字 '%s' 被使用, 但全文件都没导入/定义"
                        % (lineno, name))
    return (not problems), problems


def main(argv):
    quiet = "--quiet" in argv
    include_tests = "--all" in argv
    explicit = [a for a in argv[1:] if not a.startswith("-")]

    if explicit:
        files = [os.path.abspath(a) for a in explicit]
    else:
        files = iter_py(include_tests)
        for extra in EXTRA_RUNTIME:                   # tests/ 里的运行时入口别漏
            if extra not in files:
                files.append(extra)
    if not files:
        print("preflight: 没找到要检查的 .py", file=sys.stderr)
        return 1

    bad = 0
    for path in files:
        ok, problems = check_file(path)
        rel = os.path.relpath(path, REPO) if path.startswith(REPO) else path
        if ok:
            if not quiet:
                print("  ✓ %s" % rel)
        else:
            bad += 1
            print("  ✗ %s" % rel)
            for p in problems[:12]:
                print("      " + p)
            if len(problems) > 12:
                print("      ... 还有 %d 条" % (len(problems) - 12))

    if bad:
        print("\npreflight 失败: %d/%d 个文件有问题 —— 先修好再重启服务, "
              "否则开机就是崩溃循环" % (bad, len(files)))
        return 1
    if not quiet:
        print("\npreflight 通过: %d 个文件 ✓" % len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
