"""可单独启动的手机轻量审核页。"""

from __future__ import annotations

import html
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from src.config import OUTPUT_DIR
from src.services.mobile_review import MobileReviewService


app = FastAPI(title="映证移动审核", docs_url=None, redoc_url=None)
service = MobileReviewService()


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f5f7fb;color:#172033;margin:0;padding:20px}}
main{{max-width:720px;margin:auto;background:white;border-radius:18px;padding:24px;box-shadow:0 12px 40px #24314d18}}
.clip{{border:1px solid #e5e9f2;border-radius:12px;padding:14px;margin:12px 0}}button{{width:100%;padding:14px;border:0;border-radius:10px;font-size:16px;margin-top:10px}}
.approve{{background:#4f46e5;color:white}}.change{{background:#eef0f5;color:#26334d}}textarea,input{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #ccd3df;border-radius:10px;margin:6px 0 12px}}
</style></head><body><main>{body}</main></body></html>""")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/review/{token}", response_class=HTMLResponse)
def review(token: str):
    try:
        _, record, plan = service.inspect(OUTPUT_DIR / "tasks", token)
    except ValueError as error:
        return _page("链接不可用", f"<h1>链接不可用</h1><p>{html.escape(str(error))}</p>")
    clips = "".join(
        f'<div class="clip"><b>片段 {item.order}</b><br>原素材 {item.source_start:.1f}–{item.source_end:.1f} 秒<br>成片 {item.output_start:.1f}–{item.output_end:.1f} 秒</div>'
        for item in plan.timeline_segments
    )
    nonce = secrets.token_urlsafe(16)
    return _page(
        "审核剪辑方案",
        f"<h1>审核剪辑方案 v{record.plan_version}</h1>"
        f"<p>预计成片 {plan.estimated_duration:.1f} 秒，共 {len(plan.timeline_segments)} 个片段。</p>{clips}"
        f'<form method="post"><input type="hidden" name="idempotency_key" value="{nonce}">'
        '<label>你的称呼（可选）</label><input name="reviewer_name">'
        '<label>审核意见（退回修改时请填写）</label><textarea name="comment" rows="4"></textarea>'
        '<button class="approve" name="decision" value="approve">批准这个版本</button>'
        '<button class="change" name="decision" value="changes_requested">退回修改</button></form>',
    )


@app.post("/review/{token}", response_class=HTMLResponse)
async def submit_review(token: str, request: Request):
    form = await request.form()
    try:
        submission = service.submit(
            tasks_root=OUTPUT_DIR / "tasks",
            token=token,
            decision=str(form.get("decision", "")),
            comment=str(form.get("comment", "")),
            reviewer_name=str(form.get("reviewer_name", "")),
            idempotency_key=str(form.get("idempotency_key", "")),
        )
    except ValueError as error:
        return _page("提交失败", f"<h1>提交失败</h1><p>{html.escape(str(error))}</p>")
    message = "已批准，操作者可以继续导出。" if submission.decision == "approve" else "已退回修改。"
    return _page("审核已提交", f"<h1>审核已提交</h1><p>{message}</p>")
