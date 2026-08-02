# Day 01 实验记录

## 实验一：项目测试基线

### 实验目标

确认当前项目在学习开始时能否通过自动化测试。

### 使用命令

```bash
.venv/bin/python -m pytest -q
```

### 预期结果

所有当前测试通过。

### 实际结果

错误现象：
直接输入项目目录时出现permission denied；
在用户主目录运行.venv/bin/python时显示文件不存在。

根因：
把目录路径当成命令执行；
相对路径从错误的当前目录开始查找。

解决：
先使用cd进入项目目录，再运行项目虚拟环境中的Python。

### 失败时的错误摘要

待填写。

## 实验二：离线评测基线

### 实验目标

观察项目当前记录了哪些 AI 效果指标。

### 使用命令

```bash
.venv/bin/python scripts/run_eval.py
```

### 预期结果

生成或更新 `output/evaluation_report.json`。

### 实际结果

待填写。

### 我看到的指标

- Recall@K：
- MRR：
- NDCG：
- 引用有效率：
- 幻觉率：
- 延迟：

## 实验三：启动界面或 CLI

### 使用命令

```bash
.venv/bin/python run.py --ui
```

### 实际结果

待填写。

### 我完成的用户操作

1. 待填写。

## 今日实验结论

待填写。

