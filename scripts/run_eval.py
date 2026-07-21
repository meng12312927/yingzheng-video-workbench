"""运行 MVP2 离线评测并写入 output/evaluation_report.json。"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.evaluation import OfflineEvaluationRunner  # noqa: E402


if __name__ == "__main__":
    dataset = PROJECT_ROOT / "eval" / "mvp2_dataset.json"
    output = PROJECT_ROOT / "output" / "evaluation_report.json"
    report = OfflineEvaluationRunner().run_to_file(dataset, output, top_k=3)
    print(f"任务数: {report['task_count']}")
    for name, metrics in report["variants"].items():
        print(
            f"{name}: Recall@3={metrics['must_recall_at_k']:.3f}, "
            f"MRR={metrics['mrr']:.3f}, NDCG@3={metrics['ndcg_at_k']:.3f}, "
            f"P95={metrics['latency_p95_ms']:.2f}ms"
        )
    print(f"报告: {output}")

